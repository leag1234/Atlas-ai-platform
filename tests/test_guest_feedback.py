"""Synthetic regressions for the four owner-reported defects, 2026-10-25."""

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from infra.openwebui_images import Filter
from services.orchestrator.chat_api import app
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.image_store import prepare_uploads
from services.orchestrator.side_view import pdf_placeholders
from test_m23_documents import file_part
from test_uploaded_images import picture


class TurnScopeTests(unittest.TestCase):
    def history(self, question: str) -> dict[str, Any]:
        return {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Summarize this proposal"},
                        file_part(
                            "willow-harbor.txt", b"Budget 730 euros. Review 19 samples."
                        ),
                        file_part("annex.txt", b"Delivery in November."),
                    ],
                },
                {"role": "assistant", "content": "The proposal needs 730 euros."},
                {"role": "user", "content": question},
            ]
        }

    def test_unrelated_turn_does_not_read_old_file_parts(self) -> None:
        for question in (
            "create an image of a cartoon rabbit with a pink nose and a halo",
            "draw me a diagram of a regular 1-floor apartment in the Netherlands",
            "pls describe this photo",
            "how are you doing today and would you recommend riding a bike this time of year in the Netherlands?",
        ):
            with self.subTest(question=question):
                raw, _, _ = prepare_uploads(self.history(question))
                request = ChatRequest.model_validate(raw)
                self.assertFalse(request.has_attachments)
                self.assertEqual(request.timeout_seconds, 120)
                self.assertEqual(pdf_placeholders(request), "")

    def test_document_followups_reuse_without_announcing(self) -> None:
        for question in (
            "What is its budget?",
            "What does page 8 say?",
            "RESUME CE DOCUMENT",
            "Explain the risks in that proposal",
        ):
            with self.subTest(question=question):
                raw, _, _ = prepare_uploads(self.history(question))
                request = ChatRequest.model_validate(raw)
                self.assertEqual(len(request.documents), 2)
                self.assertTrue(all(d.reused for d in request.documents))
                self.assertEqual(pdf_placeholders(request), "")

    def test_named_reference_selects_only_named_document(self) -> None:
        raw, _, _ = prepare_uploads(self.history("Summarize annex.txt"))
        self.assertEqual([d["filename"] for d in raw["documents"]], ["annex.txt"])

    def test_new_document_stays_fresh(self) -> None:
        raw = self.history("Summarize this")
        raw["messages"][-1]["content"] = [file_part("fresh.txt", b"New evidence")]
        value, _, _ = prepare_uploads(raw)
        self.assertEqual([d["filename"] for d in value["documents"]], ["fresh.txt"])


class InletScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_files_are_index_not_new_uploads(self) -> None:
        entry = {"type": "file", "id": "old-id", "name": "willow-harbor.pdf"}
        chat = SimpleNamespace(
            chat={
                "history": {
                    "messages": {
                        "old": {
                            "role": "user",
                            "files": [entry],
                            "parentId": None,
                            "content": "Summarize this proposal",
                        },
                        "answer": {
                            "role": "assistant",
                            "parentId": "old",
                            "content": "Budget 730 euros",
                        },
                        "current": {
                            "role": "user",
                            "files": [],
                            "parentId": "answer",
                            "content": "Draw a bicycle",
                        },
                    }
                }
            }
        )
        chats = SimpleNamespace(get_chat_by_id_and_user_id=AsyncMock(return_value=chat))
        modules = {"open_webui.models.chats": SimpleNamespace(Chats=chats)}
        with patch(
            "infra.openwebui_images.import_module", side_effect=modules.__getitem__
        ):
            body = await Filter().inlet(
                {
                    "model": "atlas-qwen",
                    "files": [entry],
                    "messages": [{"role": "user", "content": "Draw a bicycle"}],
                },
                __user__={"id": "owner"},
                __metadata__={"chat_id": "chat", "user_message_id": "current"},
            )
        self.assertEqual(body["files"], [])
        self.assertEqual(body["messages"][-1]["content"], "Draw a bicycle")
        chats.get_chat_by_id_and_user_id.assert_awaited_once_with("chat", "owner")

    async def test_other_users_image_file_rejected_before_storage(self) -> None:
        chat = SimpleNamespace(
            chat={
                "history": {
                    "messages": {
                        "current": {
                            "role": "user",
                            "content": "Describe this photo",
                            "files": [{"type": "image", "id": "other-image"}],
                        }
                    }
                }
            }
        )
        storage = SimpleNamespace(
            get_file=AsyncMock(side_effect=AssertionError("must not read"))
        )
        modules = {
            "open_webui.models.chats": SimpleNamespace(
                Chats=SimpleNamespace(
                    get_chat_by_id_and_user_id=AsyncMock(return_value=chat)
                )
            ),
            "open_webui.models.files": SimpleNamespace(
                Files=SimpleNamespace(
                    get_file_by_id=AsyncMock(
                        return_value=SimpleNamespace(user_id="other")
                    )
                )
            ),
            "open_webui.storage.provider": SimpleNamespace(Storage=storage),
        }
        with (
            patch(
                "infra.openwebui_images.import_module", side_effect=modules.__getitem__
            ),
            self.assertRaisesRegex(ValueError, "unavailable"),
        ):
            await Filter().inlet(
                {
                    "model": "atlas-qwen",
                    "messages": [{"role": "user", "content": "Describe this photo"}],
                },
                __user__={"id": "owner"},
                __metadata__={"chat_id": "chat", "user_message_id": "current"},
            )
        storage.get_file.assert_not_called()


class ImageOwnershipTests(unittest.TestCase):
    def test_two_users_cannot_materialize_each_others_image(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {
                    "ATLAS_IMAGE_DIR": root + "/images",
                    "ATLAS_INTERACTION_DIR": root + "/logs",
                },
            ),
            TestClient(app) as client,
        ):
            first = client.post(
                "/images/upload",
                json={"url": picture()},
                headers={"X-OpenWebUI-User-Id": "alice"},
            )
            second = client.post(
                "/images/upload",
                json={"url": picture()},
                headers={"X-OpenWebUI-User-Id": "bob"},
            )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            self.assertNotEqual(first.json()["url"], second.json()["url"])
            payload = {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Describe this image"},
                            {
                                "type": "image_url",
                                "image_url": {"url": first.json()["url"]},
                            },
                        ],
                    }
                ]
            }
            with patch(
                "services.orchestrator.chat_api.process",
                AsyncMock(side_effect=AssertionError("must not reach inference")),
            ):
                reply = client.post(
                    "/v1/chat/completions",
                    json=payload,
                    headers={"X-OpenWebUI-User-Id": "bob"},
                )
            self.assertEqual(reply.status_code, 400)
            self.assertEqual(
                client.get(
                    "/images/" + first.json()["url"].rsplit("/", 1)[-1],
                    headers={"X-OpenWebUI-User-Id": "bob"},
                ).status_code,
                404,
            )


class RoutingTests(unittest.TestCase):
    def test_exact_person_starts_with_one_quoted_query(self) -> None:
        from services.orchestrator.search_policy import required_research

        for question in (
            "Who is Mira Alderwick?",
            "Tell me about Mira Alderwick, who is this person?",
            "MIRA ALDERWICK BIOGRAPHY",
            "Find Mira Alderwick's work",
            "Qui est Mira Alderwick ?",
        ):
            with self.subTest(question=question):
                calls = required_research(question, "en")
                self.assertEqual(len(calls), 1)
                self.assertTrue(
                    all(
                        '"mira alderwick"'
                        in json.loads(c.arguments)["query"].casefold()
                        for c in calls
                    )
                )
        self.assertEqual(required_research("Do not look up a person", "en"), ())

    def test_spatial_requests_select_svg(self) -> None:
        from services.orchestrator.appearance import is_simple_drawing_request
        from services.orchestrator.diagram import requested

        for question in (
            "draw me a diagram of a regular 1-floor apartment in the Netherlands",
            "Sketch a room layout",
            "Apartment floor plan",
            "DESSINE UN PLAN D APPARTEMENT",
            "Show how the rooms fit together",
        ):
            with self.subTest(question=question):
                self.assertTrue(is_simple_drawing_request(question))
                self.assertFalse(requested(question))
        self.assertFalse(is_simple_drawing_request("Do not draw a floor plan"))


class PersonEvidenceTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def model() -> SimpleNamespace:
        return SimpleNamespace(
            tools=[],
            observing=False,
            sink=None,
            local_enabled=False,
            configure_quality=Mock(),
            provider_retries=0,
            input_tokens=40,
            output_tokens=20,
            provider_model="recorded-person-model",
            reasoning="",
            trace_tokens=0,
            answer_tokens=20,
            token_split_estimated=False,
            reasoning_retried=False,
            generation_ms=1.0,
            observations=[],
        )

    def test_second_pass_uses_plain_context_for_unsupported_exclusions(self) -> None:
        from services.orchestrator.person_lookup import second_query

        passages = ["Mira Alderwick founded Cobalt Forge and Lumen OS."]
        for provider in ("tavily", "searxng"):
            with self.subTest(provider=provider):
                call = second_query("Mira Alderwick", passages, "en", provider)
                assert call is not None
                self.assertEqual(
                    json.loads(call.arguments)["query"],
                    '"Mira Alderwick" biography profile career',
                )

    def test_provider_shaped_fictional_results_keep_subject_titles_and_context(
        self,
    ) -> None:
        from services.orchestrator.person_lookup import _subject_passage, _subject_title

        fixture = json.loads(
            Path("tests/fixtures/person-search-results.json").read_text()
        )
        serp_row = fixture["serpapi"]["data"]["results"][0]
        searx_row = fixture["searxng"]["data"]["results"][0]
        self.assertTrue(_subject_title("Élise Marceau", serp_row["title"]))
        self.assertTrue(_subject_passage("Élise Marceau", serp_row["snippet"]))
        self.assertTrue(_subject_passage("Élise Marceau", searx_row["content"]))

    async def test_consistent_exact_sources_are_written_by_model_with_citations(
        self,
    ) -> None:
        from services.orchestrator.chat_pipeline import ChatTools
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.loop import Result
        from services.orchestrator.person_lookup import lookup

        request = ChatRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "Who is Lorin Vasquel?"}],
                "lang": "en",
            }
        )
        item = Interaction()
        tools = ChatTools(item, request.user_text, allow_image=False)
        response = {
            "data": {
                "results": [
                    {
                        "snippet": "Lorin Vasquel founded Northwind Studio and Bay OS.",
                        "link": "https://example.test/profile",
                    },
                    {
                        "snippet": "Lorin Vasquel is the founder of Bay OS and Harbor Devices.",
                        "link": "https://example.test/company",
                    },
                ]
            }
        }
        model = self.model()
        with (
            patch.object(tools, "execute", AsyncMock(return_value=response)),
            patch(
                "services.orchestrator.person_lookup.GatewayModel.connect",
                new=AsyncMock(return_value=model),
            ),
            patch(
                "services.orchestrator.person_lookup.run",
                new=AsyncMock(
                    return_value=Result(
                        "Lorin Vasquel is an entrepreneur who founded Northwind Studio, "
                        "Bay OS, and Harbor Devices [1](https://example.test/profile) "
                        "[2](https://example.test/company)."
                    )
                ),
            ) as answer,
        ):
            await lookup(request, item, tools, "Lorin Vasquel")
        self.assertIn("founded Northwind Studio", item.reponse)
        self.assertIn("[1](https://example.test/profile)", item.reponse)
        self.assertEqual(
            item.retrieved_urls,
            ["https://example.test/profile", "https://example.test/company"],
        )
        assert answer.await_args is not None
        evidence = json.loads(answer.await_args.kwargs["history"][0]["content"])
        self.assertEqual(
            evidence["untrusted_exact_name_evidence"],
            [
                {
                    "url": "https://example.test/profile",
                    "passages": ["Lorin Vasquel founded Northwind Studio and Bay OS."],
                    "source_kind": "profile",
                },
                {
                    "url": "https://example.test/company",
                    "passages": [
                        "Lorin Vasquel is the founder of Bay OS and Harbor Devices."
                    ],
                    "source_kind": "profile",
                },
            ],
        )
        self.assertNotIn("What profession", item.reponse)

    async def test_mixed_exact_namesakes_are_judged_by_model(self) -> None:
        from services.orchestrator.chat_pipeline import ChatTools
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.loop import Result
        from services.orchestrator.person_lookup import lookup

        request = ChatRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "Who is Mira Alderwick?"}],
                "lang": "en",
            }
        )
        item = Interaction()
        tools = ChatTools(item, request.user_text, allow_image=False)
        response = {
            "data": {
                "results": [
                    {
                        "snippet": "Mira Alderwick is a botanist in Glasgow.",
                        "link": "https://example.test/botanist",
                    },
                    {
                        "snippet": "Mira Alderwick is a novelist in Lisbon.",
                        "link": "https://example.test/novelist",
                    },
                ]
            }
        }
        with (
            patch.object(tools, "execute", AsyncMock(return_value=response)),
            patch(
                "services.orchestrator.person_lookup.GatewayModel.connect",
                new=AsyncMock(return_value=self.model()),
            ),
            patch(
                "services.orchestrator.person_lookup.run",
                new=AsyncMock(
                    return_value=Result(
                        "Mira Alderwick is a botanist in Glasgow [1](https://example.test/botanist); "
                        "Mira Alderwick is a novelist in Lisbon [2](https://example.test/novelist). "
                        "What profession, location, or date do you mean? "
                    )
                ),
            ),
        ):
            await lookup(request, item, tools, "Mira Alderwick")
        self.assertIn("botanist in Glasgow", item.reponse)
        self.assertIn("novelist in Lisbon", item.reponse)
        self.assertIn("What profession", item.reponse)

    async def test_directory_result_does_not_create_an_extra_identity(self) -> None:
        from services.orchestrator.chat_pipeline import ChatTools
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.loop import Result
        from services.orchestrator.person_lookup import lookup

        item = Interaction()
        request = ChatRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "Who is Mira Alderwick?"}],
                "lang": "en",
            }
        )
        tools = ChatTools(item, request.user_text, allow_image=False)
        response = {
            "data": {
                "results": [
                    {
                        "snippet": "Mira Alderwick is a botanist in Glasgow.",
                        "link": "https://example.test/botanist",
                    },
                    {
                        "title": "People directory results",
                        "snippet": "People directory profiles matching Mira Alderwick.",
                        "link": "https://directory.example.test/search/people",
                    },
                ]
            }
        }
        answer = AsyncMock(
            return_value=Result(
                "Mira Alderwick is a botanist in Glasgow [1](https://example.test/botanist)."
            )
        )
        with (
            patch.object(tools, "execute", AsyncMock(return_value=response)),
            patch(
                "services.orchestrator.person_lookup.GatewayModel.connect",
                new=AsyncMock(return_value=self.model()),
            ),
            patch("services.orchestrator.person_lookup.run", new=answer),
        ):
            await lookup(request, item, tools, "Mira Alderwick")
        self.assertIn("botanist in Glasgow", item.reponse)
        self.assertNotIn("different people", item.reponse)
        assert answer.await_args is not None
        payload = json.loads(answer.await_args.kwargs["history"][0]["content"])
        self.assertEqual(len(payload["untrusted_exact_name_evidence"]), 1)

    async def test_namesake_does_not_become_the_answer(self) -> None:
        from services.orchestrator.chat_pipeline import ChatTools
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.person_lookup import lookup

        item = Interaction()
        request = ChatRequest.model_validate(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "Tell me about Mira Alderwick, who is this person?",
                    }
                ],
                "lang": "en",
            }
        )
        tools = ChatTools(item, request.user_text, allow_image=False)
        response = {
            "data": {
                "results": [
                    {
                        "title": "Theo Alderwick footballer",
                        "snippet": "Theo Alderwick plays for Fictional FC and is 1.82 m tall.",
                        "link": "https://example.test/theo",
                    }
                ]
            }
        }
        with patch.object(tools, "execute", AsyncMock(return_value=response)) as search:
            await lookup(request, item, tools, "Mira Alderwick")
        self.assertEqual(search.await_count, 2)
        self.assertIn("could not find reliable", item.reponse)
        self.assertIn('"Mira Alderwick"', item.reponse)
        self.assertIn("not the same person", item.reponse)
        self.assertNotIn("Fictional FC", item.reponse)
        self.assertNotIn("1.82", item.reponse)

    async def test_exact_match_quotes_only_retrieved_facts(self) -> None:
        from services.orchestrator.chat_pipeline import ChatTools
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.loop import Result
        from services.orchestrator.person_lookup import lookup

        item = Interaction()
        request = ChatRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "Who is Mira Alderwick?"}],
                "lang": "en",
            }
        )
        tools = ChatTools(item, request.user_text, allow_image=False)
        response = {
            "data": {
                "results": [
                    {
                        "snippet": "Mira Alderwick designs fictional gardens. Theo Alderwick plays football.",
                        "link": "https://example.test/mira",
                    }
                ]
            }
        }
        answer = AsyncMock(
            return_value=Result(
                "Mira Alderwick designs fictional gardens. [1](https://example.test/mira)"
            )
        )
        with (
            patch.object(tools, "execute", AsyncMock(return_value=response)),
            patch(
                "services.orchestrator.person_lookup.GatewayModel.connect",
                new=AsyncMock(return_value=self.model()),
            ),
            patch("services.orchestrator.person_lookup.run", new=answer),
        ):
            await lookup(request, item, tools, "Mira Alderwick")
        self.assertIn("Mira Alderwick designs fictional gardens.", item.reponse)
        self.assertNotIn("plays football", item.reponse)
        self.assertNotIn("height", item.reponse)
        assert answer.await_args is not None
        payload = json.loads(answer.await_args.kwargs["history"][0]["content"])
        self.assertNotIn("Theo Alderwick", json.dumps(payload))

    async def test_second_pass_uses_exclusions_to_find_other_identity(self) -> None:
        from services.orchestrator.chat_pipeline import ChatTools
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.loop import Result
        from services.orchestrator.person_lookup import lookup

        item = Interaction()
        request = ChatRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "Who is Mira Alderwick?"}],
                "lang": "en",
            }
        )
        tools = ChatTools(item, request.user_text, allow_image=False)
        first = {
            "data": {
                "provider": "serpapi",
                "results": [
                    {
                        "snippet": "Mira Alderwick founded Cobalt Forge and Lumen OS.",
                        "link": "https://example.test/cobalt",
                    }
                ],
            }
        }
        second = {
            "data": {
                "provider": "serpapi",
                "results": [
                    {
                        "snippet": "Mira Alderwick is a marine biologist at Tide Institute.",
                        "link": "https://example.test/tide",
                    }
                ],
            }
        }
        answer = AsyncMock(
            return_value=Result("Two people share this exact full name.")
        )
        with (
            patch.object(
                tools, "execute", AsyncMock(side_effect=[first, second])
            ) as search,
            patch(
                "services.orchestrator.person_lookup.GatewayModel.connect",
                new=AsyncMock(return_value=self.model()),
            ),
            patch("services.orchestrator.person_lookup.run", new=answer),
        ):
            await lookup(request, item, tools, "Mira Alderwick")
        second_call = search.await_args_list[1].args[0]
        self.assertEqual(search.await_count, 2)
        self.assertEqual(
            json.loads(second_call.arguments)["query"],
            '"Mira Alderwick" -cobalt -forge -lumen',
        )
        self.assertEqual(
            item.retrieved_urls,
            ["https://example.test/cobalt", "https://example.test/tide"],
        )

    async def test_second_pass_uses_unfiltered_first_result_material(self) -> None:
        from services.orchestrator.chat_pipeline import ChatTools
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.person_lookup import lookup

        request = ChatRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "Who is Mira Alderwick?"}],
                "lang": "en",
            }
        )
        item = Interaction()
        tools = ChatTools(item, request.user_text, allow_image=False)
        first = {
            "data": {
                "provider": "serpapi",
                "results": [
                    {
                        "title": "Alderwick family directory",
                        "snippet": "A directory lists Mira Alderwick beside unrelated records.",
                        "link": "https://directory.example.test/alderwick",
                    }
                ],
            }
        }
        with patch.object(
            tools, "execute", AsyncMock(side_effect=[first, {"data": {"results": []}}])
        ) as search:
            await lookup(request, item, tools, "Mira Alderwick")
        self.assertEqual(search.await_count, 2)
        self.assertIn(
            "-directory",
            json.loads(search.await_args_list[1].args[0].arguments)["query"],
        )

    async def test_namesake_list_and_passing_mentions_are_not_cited(self) -> None:
        from services.orchestrator.chat_pipeline import ChatTools
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.loop import Result
        from services.orchestrator.person_lookup import lookup

        item = Interaction()
        request = ChatRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "Who is Mira Alderwick?"}],
                "lang": "en",
            }
        )
        tools = ChatTools(item, request.user_text, allow_image=False)
        response = {
            "data": {
                "provider": "serpapi",
                "results": [
                    {
                        "snippet": "Mira Alderwick designs fictional gardens.",
                        "link": "https://example.test/profile",
                    },
                    {
                        "snippet": "Alderwick is a surname shared by Theo Alderwick and Mira Alderwick.",
                        "link": "https://example.test/surname",
                    },
                    {
                        "snippet": "The band thanked Mira Alderwick for lending a phone.",
                        "link": "https://example.test/band",
                    },
                    {
                        "snippet": "A social post mentions Mira Alderwick beside a phone review.",
                        "link": "https://example.test/social",
                    },
                ],
            }
        }
        answer = AsyncMock(
            return_value=Result("Mira Alderwick designs fictional gardens.")
        )
        with (
            patch.object(tools, "execute", AsyncMock(return_value=response)),
            patch(
                "services.orchestrator.person_lookup.GatewayModel.connect",
                new=AsyncMock(return_value=self.model()),
            ),
            patch("services.orchestrator.person_lookup.run", new=answer),
        ):
            await lookup(request, item, tools, "Mira Alderwick")
        assert answer.await_args is not None
        payload = json.loads(answer.await_args.kwargs["history"][0]["content"])
        self.assertEqual(
            [source["url"] for source in payload["untrusted_exact_name_evidence"]],
            ["https://example.test/profile"],
        )
        self.assertEqual(item.retrieved_urls, ["https://example.test/profile"])

    async def test_person_synthesis_uses_question_language(self) -> None:
        from services.orchestrator.chat_pipeline import ChatTools
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.loop import Result
        from services.orchestrator.person_lookup import lookup

        questions = {
            "fr": "Qui est Mira Alderwick ?",
            "de": "Wer ist Mira Alderwick?",
            "es": "Quien es Mira Alderwick?",
            "it": "Chi e Mira Alderwick?",
        }
        names = {"fr": "French", "de": "German", "es": "Spanish", "it": "Italian"}
        response = {
            "data": {
                "provider": "tavily",
                "results": [
                    {
                        "snippet": "Mira Alderwick is a novelist in Lisbon.",
                        "link": "https://example.test/profile",
                    }
                ],
            }
        }
        for language, question in questions.items():
            with self.subTest(language=language):
                item = Interaction()
                request = ChatRequest.model_validate(
                    {
                        "messages": [{"role": "user", "content": question}],
                        "lang": language,
                    }
                )
                tools = ChatTools(item, request.user_text, allow_image=False)

                async def fake_model(
                    query: object,
                    _model: object,
                    _tools: object,
                    instruction: str,
                    _limits: object,
                    **_: object,
                ) -> Result:
                    self.assertEqual(getattr(query, "lang"), language)
                    self.assertIn(f"{names[language]} ({language})", instruction)
                    self.assertIn(f"Reply in {names[language]}", instruction)
                    return Result("localized answer")

                with (
                    patch.object(tools, "execute", AsyncMock(return_value=response)),
                    patch(
                        "services.orchestrator.person_lookup.GatewayModel.connect",
                        new=AsyncMock(return_value=self.model()),
                    ),
                    patch("services.orchestrator.person_lookup.run", new=fake_model),
                ):
                    await lookup(request, item, tools, "Mira Alderwick")
                self.assertEqual(item.reponse, "localized answer")


class VisionDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_jpeg_file_is_sent_to_vision_not_document_reading(self) -> None:
        photo = {
            "type": "file",
            "id": "photo",
            "name": "dog-swimming.jpg",
            "content_type": "image/jpeg",
        }
        chat = SimpleNamespace(
            chat={
                "history": {
                    "messages": {
                        "current": {
                            "role": "user",
                            "content": "What is in this photo?",
                            "files": [photo],
                        }
                    }
                }
            }
        )
        file = SimpleNamespace(
            user_id="owner",
            path="unused",
            meta={"name": "dog-swimming.jpg", "content_type": "image/jpeg"},
            filename="dog-swimming.jpg",
        )
        modules = {
            "open_webui.models.chats": SimpleNamespace(
                Chats=SimpleNamespace(
                    get_chat_by_id_and_user_id=AsyncMock(return_value=chat)
                )
            ),
            "open_webui.models.files": SimpleNamespace(
                Files=SimpleNamespace(get_file_by_id=AsyncMock(return_value=file))
            ),
            "open_webui.storage.provider": SimpleNamespace(
                Storage=SimpleNamespace(get_file=lambda _: "unused")
            ),
        }
        filter_ = Filter()
        with (
            patch(
                "infra.openwebui_images.import_module", side_effect=modules.__getitem__
            ),
            patch.object(
                filter_, "owned_file", AsyncMock(return_value=(file, b"synthetic-jpeg"))
            ),
            patch.object(
                filter_,
                "upload",
                AsyncMock(
                    return_value="https://images.test/images/0123456789abcdef0123456789abcdef"
                ),
            ),
        ):
            body = await filter_.inlet(
                {
                    "model": "atlas-qwen",
                    "messages": [{"role": "user", "content": "What is in this photo?"}],
                    "files": [photo],
                },
                __user__={"id": "owner"},
                __metadata__={"chat_id": "chat", "user_message_id": "current"},
            )
        content = body["messages"][-1]["content"]
        self.assertEqual(body["files"], [])
        self.assertTrue(any(part["type"] == "image_url" for part in content))
        self.assertFalse(any(part["type"] == "file" for part in content))

    async def test_current_pixels_survive_document_history(self) -> None:
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.vision import process_vision
        from services.orchestrator.model import GatewayModel

        image = picture(3)
        value = TurnScopeTests().history("pls describe this photo")
        value["messages"][-1]["content"] = [
            {"type": "text", "text": "pls describe this photo"},
            {"type": "image_url", "image_url": {"url": image}},
        ]
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_IMAGE_DIR": root}),
        ):
            raw, _, _ = prepare_uploads(value)
            request = ChatRequest.model_validate(raw)
        answer = {
            "text": "The synthetic image is blue.",
            "usage": {"prompt_tokens": 20, "completion_tokens": 8},
            "cost_eur": "0",
            "observation": {"provider": "escalade", "route": "complexe"},
        }
        with patch.object(GatewayModel, "post", AsyncMock(return_value=answer)) as post:
            item = Interaction()
            await process_vision(request, item)
        sent = post.call_args.args[1]["messages"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["content"][1]["image_url"]["url"], image)
        self.assertNotIn("willow-harbor", json.dumps(sent))
        self.assertEqual(item.reponse, answer["text"])

    async def test_failed_vision_is_disclosed_without_text_inference(self) -> None:
        from services.orchestrator.chat_pipeline import process
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.model import GatewayError, GatewayModel

        request = ChatRequest.model_validate(
            {
                "ui_locale": "en",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "pls describe this photo"},
                            {"type": "image_url", "image_url": {"url": picture()}},
                        ],
                    }
                ],
            }
        )
        with (
            patch.object(
                GatewayModel,
                "post",
                AsyncMock(side_effect=GatewayError("vision_unavailable", 503)),
            ),
            patch.object(
                GatewayModel,
                "connect",
                AsyncMock(
                    side_effect=AssertionError("unseeing model must not describe")
                ),
            ),
        ):
            item = Interaction()
            with self.assertRaises(GatewayError) as failure:
                await process(request, item)
        self.assertEqual(failure.exception.code, "vision_unavailable")
        self.assertEqual(item.reponse, "")


class HistoricalImageTests(unittest.TestCase):
    def test_old_webui_image_id_does_not_break_next_unrelated_turn(self) -> None:
        value = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this photo"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "/api/v1/files/00000000-0000-4000-8000-000000000001/content"
                            },
                        },
                    ],
                },
                {"role": "assistant", "content": "A synthetic dog swimming"},
                {"role": "user", "content": "Would you recommend cycling?"},
            ]
        }
        with patch(
            "services.orchestrator.image_store.load_uploaded",
            side_effect=AssertionError("must not read an old image"),
        ):
            raw, uploads, references = prepare_uploads(value)
        request = ChatRequest.model_validate(raw)
        self.assertFalse(any(m.images for m in request.messages))
        self.assertEqual(uploads, [])
        self.assertEqual(references, [])
