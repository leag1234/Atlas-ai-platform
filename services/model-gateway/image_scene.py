"""Typed scene updates and subject-bound constraints for image conversations."""

import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from image_constraints import extract_constraints, normalized


def character_key(name: str) -> str:
    return {"virgin mary": "mary"}.get(normalized(name), normalized(name))


class Character(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=100)
    sex: Literal["woman", "man", "person"]
    age_group: Literal["infant", "child", "young adult", "adult", "older adult"]
    role: str = Field(default="", max_length=200)
    attributes: list[str] = Field(default_factory=list, max_length=12)
    visual_description: str = Field(default="", max_length=200)

    def is_human(self) -> bool:
        """Formatter output sometimes mislabels an animal as a generic person."""
        return not bool(
            re.search(
                r"\b(?:animal|sheep|lamb|dog|cat|horse|cow|goat|bird|fish|"
                r"mouton|chien|chat|cheval|vache|chevre|oiseau|poisson)\b",
                " ".join((self.name, self.role, self.visual_description)),
                re.I,
            )
        )


class ActionBinding(BaseModel):
    """A visible relation whose participants remain explicit after corrections."""

    model_config = ConfigDict(extra="forbid", strict=True)
    subject: str = Field(min_length=1, max_length=100)
    action: str = Field(min_length=1, max_length=100)
    object: str = Field(min_length=1, max_length=100)

    def render(self) -> str:
        return f"{self.subject} {self.action} {self.object}"


class CountConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    count: int = Field(ge=0, le=99)
    subject: str = Field(min_length=1, max_length=100)
    owner: str = Field(default="", max_length=100)
    requested_detail: str = Field(default="", max_length=100)

    def render(self) -> str:
        names = {
            0: "zero",
            1: "one",
            2: "two",
            3: "three",
            4: "four",
            5: "five",
            6: "six",
            7: "seven",
            8: "eight",
            9: "nine",
            10: "ten",
            11: "eleven",
            12: "twelve",
        }
        written = names.get(self.count, str(self.count))
        pairs = (
            f", arranged as {names.get(self.count // 2, str(self.count // 2))} pairs"
            if self.subject == "legs" and self.count % 2 == 0
            else ""
        )
        constraint = f"exactly {written} ({self.count}) {self.subject}{pairs}, all clearly visible"
        return f"{self.owner} with {constraint}" if self.owner else constraint

    def disclosure(self) -> str:
        """Keep a verification miss in the language and terms the user chose."""
        return self.requested_detail or f"{self.count} {self.subject}"


class ScenePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    relation: Literal["edit", "new"] = "edit"
    characters: list[Character] = Field(default_factory=list, max_length=12)
    action: str | None = Field(default=None, max_length=400)
    setting: str | None = Field(default=None, max_length=400)
    elements: list[str] = Field(default_factory=list, max_length=16)
    style: str | None = Field(default=None, max_length=200)
    remove_characters: list[str] = Field(default_factory=list, max_length=12)
    remove_elements: list[str] = Field(default_factory=list, max_length=16)
    action_bindings: list[ActionBinding] = Field(default_factory=list, max_length=12)
    anatomy: bool = False

    def for_correction(self, text: str) -> "ScenePatch":
        """Discard model-proposed replacements that the correction did not request."""
        correction = normalized(text)
        changed = self.model_dump()
        if not re.search(
            r"\b(?:style|photo(?:graph)?|realistic|realiste|illustration|drawing|"
            r"dessin|cartoon|icon|icone|renaissance|painting|peinture|"
            r"noir et blanc|black and white|watercolou?r)\b",
            correction,
        ):
            changed["style"] = None
        if not re.search(
            r"\b(?:setting|scene|background|decor|d[eé]cor|etable|stable|"
            r"bethlehem|outside|outside|dans|into|background)\b",
            correction,
        ):
            changed["setting"] = None
        if not re.search(
            r"\b(?:remove|without|sans|enleve|retire|supprime|ohne|sin|senza)\b",
            correction,
        ):
            changed["remove_characters"] = []
            changed["remove_elements"] = []
        return ScenePatch.model_validate(changed)


class Scene(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    characters: list[Character] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)
    action: str = ""
    setting: str = ""
    elements: list[str] = Field(default_factory=list)
    style: str = ""
    action_bindings: list[ActionBinding] = Field(default_factory=list)
    anatomy: bool = True
    birth_requested: bool = False
    birth_moment: str = ""
    artistic_style: bool = False
    requested_iconography: list[str] = Field(default_factory=list)
    count_constraints: list[CountConstraint] = Field(default_factory=list)

    def add_count_constraints(self, source: str) -> None:
        words = {
            "zero": 0,
            "one": 1,
            "two": 2,
            "three": 3,
            "four": 4,
            "five": 5,
            "six": 6,
            "seven": 7,
            "eight": 8,
            "nine": 9,
            "ten": 10,
            "eleven": 11,
            "twelve": 12,
            "deux": 2,
            "trois": 3,
            "quatre": 4,
            "cinq": 5,
            "sept": 7,
            "huit": 8,
            "neuf": 9,
            "dix": 10,
        }
        pattern = re.compile(
            r"\b(" + "|".join([*words, r"\d{1,2}"]) + r")\s+([^\W\d_][\w-]{1,80})\b",
            re.I,
        )
        animal_characters = [
            character.name for character in self.characters if not character.is_human()
        ]
        known = {
            (constraint.count, constraint.subject, constraint.owner)
            for constraint in self.count_constraints
        }
        for value, subject in pattern.findall(source):
            count = words.get(value.lower(), int(value) if value.isdigit() else -1)
            requested_subject = subject
            subject = normalized(subject)
            bound_owner = (
                animal_characters[0]
                if len(animal_characters) == 1
                and subject
                in {
                    "leg",
                    "legs",
                    "arm",
                    "arms",
                    "head",
                    "heads",
                    "hand",
                    "hands",
                    "patte",
                    "pattes",
                    "bras",
                    "tete",
                    "tetes",
                    "main",
                    "mains",
                }
                else ""
            )
            key = (count, subject, bound_owner)
            if count >= 0 and key not in known:
                self.count_constraints.append(
                    CountConstraint(
                        count=count,
                        subject=subject,
                        owner=bound_owner,
                        requested_detail=f"{value} {requested_subject}",
                    )
                )
                known.add(key)
                if not bound_owner and subject not in self.subjects:
                    self.subjects.append(subject)
        # A formatter may have placed the literal count in Other elements. It is a
        # subject constraint, never background decoration.
        self.elements = [
            element
            for element in self.elements
            if not any(
                self._element_restates_count(element, constraint)
                for constraint in self.count_constraints
            )
        ]

    @staticmethod
    def _element_restates_count(element: str, constraint: CountConstraint) -> bool:
        """A formatted count belongs to its subject, never to scene decoration."""
        element_words = normalized(element).split()
        number_words = {
            "zero": 0,
            "one": 1,
            "two": 2,
            "three": 3,
            "four": 4,
            "five": 5,
            "six": 6,
            "seven": 7,
            "eight": 8,
            "nine": 9,
            "ten": 10,
            "eleven": 11,
            "twelve": 12,
        }
        return str(constraint.count) in element_words or any(
            number_words.get(word) == constraint.count for word in element_words
        )

    def apply(self, patch: ScenePatch) -> None:
        if patch.action and (
            self._birth_request(patch.action) or self._labor_request(patch.action)
        ):
            self.birth_requested = True
        removed = {character_key(name) for name in patch.remove_characters}
        people = {
            character_key(c.name): c
            for c in self.characters
            if character_key(c.name) not in removed
        }
        for person in patch.characters:
            key = character_key(person.name)
            previous = people.get(key)
            people[key] = Character(
                name=person.name,
                sex=person.sex,
                age_group=person.age_group,
                role=person.role or (previous.role if previous else ""),
                attributes=person.attributes
                or (previous.attributes if previous else []),
                visual_description=person.visual_description
                or (previous.visual_description if previous else ""),
            )
        self.characters = list(people.values())
        for field in ("action", "setting", "style"):
            value = getattr(patch, field)
            if value is not None:
                setattr(self, field, value)
        self.elements = list(
            dict.fromkeys(
                [e for e in self.elements if e not in patch.remove_elements]
                + patch.elements
            )
        )
        for binding in patch.action_bindings:
            if binding not in self.action_bindings:
                self.action_bindings.append(binding)
        self.anatomy |= patch.anatomy
        self.cohere_birth_moment()

    @staticmethod
    def _birth_request(text: str) -> bool:
        return bool(
            re.search(
                r"\b(?:birth|delivery|childbirth|accouch\w*|gebier\w*|gebart|da a luz|partorisc\w*)\b",
                normalized(text),
            )
        )

    @staticmethod
    def _labor_request(text: str) -> bool:
        return bool(
            re.search(
                r"\b(?:in labor|en (?:plein )?travail|avant la naissance|before (?:the )?baby arrives|before (?:the )?birth|vor der geburt|antes del parto|prima del parto)\b",
                normalized(text),
            )
        )

    def cohere_birth_moment(self, source: str = "") -> None:
        """Choose one visible birth moment before a scene reaches the renderer."""
        if self._post_birth_request(source):
            self.birth_requested = True
            self.birth_moment = "post_birth"
        elif self._labor_request(source):
            self.birth_requested = True
            self.birth_moment = "labor"
        elif self._birth_request(source):
            self.birth_requested = True
            self.birth_moment = "delivery"
        if not self.birth_requested and not (
            self._birth_request(self.action) or self._labor_request(self.action)
        ):
            return
        self.birth_requested = True
        if self.birth_moment == "labor":
            self._render_labor_moment()
        elif self.birth_moment == "post_birth":
            self._render_post_birth_moment()
        else:
            self.birth_moment = "delivery"
            self._render_delivery_moment()

    @staticmethod
    def _post_birth_request(text: str) -> bool:
        return bool(
            re.search(
                r"\b(?:after (?:the )?(?:birth|delivery|childbirth)|post[- ]birth|"
                r"apres (?:la naissance|l['’]accouchement)|nach der geburt|"
                r"despues del (?:parto|nacimiento)|dopo (?:la nascita|il parto))\b",
                normalized(text),
            )
        )

    def _render_delivery_moment(self) -> None:
        mother, newborn = self._mother(), self._newborn()
        if mother:
            mother.role = "an adult woman giving birth"
        if newborn:
            newborn.role = "a newborn baby"
        for person in self.characters:
            person.attributes = [
                value
                for value in person.attributes
                if not re.search(
                    r"pregnan|swaddl|clean|serene|calm|holding.*(?:baby|newborn)",
                    value,
                    re.I,
                )
            ]
            if person not in (mother, newborn) and self._birth_request(person.role):
                person.role = "an adult companion"
        self.action_bindings = [
            binding
            for binding in self.action_bindings
            if not re.search(
                r"(?:holds|holding|cradl|swaddl).*(?:baby|newborn|Jesus)",
                binding.render(),
                re.I,
            )
        ]
        self.action = (
            "delivery happening now: the woman half-lying on her back on straw, "
            "knees raised and apart, face contorted by effort, sweat, hair stuck to her forehead, "
            "gripping her companion's hand; companion helping receive the wet newborn "
            "at the moment of delivery, with vernix and some blood, umbilical cord still attached; "
            "blood-stained cloth and straw"
        )

    def _render_post_birth_moment(self) -> None:
        mother, newborn = self._mother(), self._newborn()
        if mother:
            mother.role = "a tired mother sitting and holding the newborn"
        if newborn:
            newborn.role = "a newborn baby"
        for character in self.characters:
            character.attributes = self._without_labor_terms(character.attributes)
            if character is not mother and re.search(
                r"pregnan|labor|giving birth|childbirth|accouch|geburt|parto",
                character.role,
                re.I,
            ):
                character.role = "an adult companion"
        self.action_bindings = [
            binding
            for binding in self.action_bindings
            if not self._birth_request(binding.render())
            and not self._labor_request(binding.render())
        ]
        self.action = "a quiet moment with the tired mother holding the newborn"
        hand_hold = next(
            (
                binding
                for binding in self.action_bindings
                if binding.subject == "Joseph"
                and binding.action == "holds"
                and binding.object == "Mary's own hand with his hand"
            ),
            None,
        )
        if hand_hold and not any(
            binding.subject == "Mary"
            and binding.action == "holds"
            and binding.object == "the newborn in her other arm"
            for binding in self.action_bindings
        ):
            self.action_bindings.append(
                ActionBinding(
                    subject="Mary",
                    action="holds",
                    object="the newborn in her other arm",
                )
            )

    def _render_labor_moment(self) -> None:
        mother = self._mother()
        if mother:
            mother.role = "a mother in labor with a visibly pregnant belly"
            mother.attributes = self._without_newborn_terms(mother.attributes)
        self.characters = [
            character
            for character in self.characters
            if character is mother
            or (
                not re.search(r"\b(?:jesus|baby|newborn)\b", character.name, re.I)
                and not re.search(r"\b(?:baby|newborn)\b", character.role, re.I)
            )
        ]
        for character in self.characters:
            character.attributes = self._without_newborn_terms(character.attributes)
        self.action_bindings = [
            binding
            for binding in self.action_bindings
            if not re.search(r"\b(?:baby|newborn|jesus)\b", binding.render(), re.I)
        ]
        self.action = "visible contractions during labor"

    def _mother(self) -> Character | None:
        return next(
            (
                character
                for character in self.characters
                if character_key(character.name) == "mary"
                or re.search(
                    r"\b(?:mother|woman)\b",
                    character.role,
                    re.I,
                )
            ),
            None,
        )

    def _newborn(self) -> Character | None:
        named = next(
            (
                character
                for character in self.characters
                if character_key(character.name) == "jesus"
            ),
            None,
        )
        return named or next(
            (
                character
                for character in self.characters
                if re.search(r"\b(?:baby|newborn)\b", character.role, re.I)
            ),
            None,
        )

    @staticmethod
    def _without_labor_terms(values: list[str]) -> list[str]:
        return [
            value
            for value in values
            if not re.search(
                r"pregnan|labor|giving birth|childbirth|accouch|geburt|parto",
                value,
                re.I,
            )
        ]

    @staticmethod
    def _without_newborn_terms(values: list[str]) -> list[str]:
        return [
            value
            for value in values
            if not re.search(r"\b(?:baby|newborn)\b", value, re.I)
        ]

    def apply_user_correction(self, text: str) -> None:
        """Give an explicit later correction precedence over inferred birth roles."""
        correction = normalized(text)
        self.set_render_intent(text)
        self.cohere_birth_moment(text)
        post_birth = bool(
            re.search(
                r"\b(?:baby|bebe|kind|nino|bambino).{0,24}"
                r"(?:already born|deja ne|schon geboren|ya nacio|gia nato)\b"
                r"|\b(?:no longer|not anymore|ne .{0,12} plus|nicht mehr|ya no|non piu)\s+"
                r"(?:pregnant|enceinte|schwanger|embarazada|incinta)\b"
                r"|\b(?:without|sans|ohne|sin|senza|remove|retire|enleve|entferne|quitar|rimuovi)"
                r".{0,24}(?:pregnan|belly|ventre|bauch|barriga|pancia|labor|accouch|geburt|parto)\b",
                correction,
            )
        )
        binding = self._hand_holding_binding(correction)
        if binding and binding not in self.action_bindings:
            self.action_bindings.append(binding)
        support = self._support_binding(correction)
        if support and support not in self.action_bindings:
            self.action_bindings.append(support)
        if binding or support:
            self.cohere_birth_moment()
        if not post_birth:
            return
        self.birth_requested = True
        self.birth_moment = "post_birth"
        for person in self.characters:
            if re.search(
                r"pregnan|labor|giving birth|childbirth|accouch|geburt|parto",
                person.role,
                re.I,
            ):
                person.role = "a parent beside the newborn baby"
            person.attributes = [
                attribute
                for attribute in person.attributes
                if not re.search(
                    r"pregnan|labor|giving birth|childbirth|accouch|geburt|parto",
                    attribute,
                    re.I,
                )
            ]
        self.cohere_birth_moment()

    def _hand_holding_binding(self, correction: str) -> ActionBinding | None:
        relation = re.search(
            r"\b(?:hold(?:s|ing)?|tient|tenir|halt(?:e|en|t)|sostiene|tiene)\b"
            r".{0,24}\b(?:hand|main|hand|mano)\b",
            correction,
        )
        if not relation:
            return None
        mentions = [
            (person.name, match.start())
            for person in self.characters
            for match in [
                re.search(rf"\b{re.escape(normalized(person.name))}\b", correction)
            ]
            if match
        ]
        subject = next(
            (
                name
                for name, position in reversed(mentions)
                if position < relation.start()
            ),
            None,
        )
        object_ = next(
            (name for name, position in mentions if position > relation.start()),
            next(
                (person.name for person in self.characters if person.name != subject),
                None,
            ),
        )
        if subject and object_:
            return ActionBinding(
                subject=subject,
                action="holds",
                object=f"{object_}'s own hand with his hand",
            )
        return None

    def _support_binding(self, correction: str) -> ActionBinding | None:
        if not re.search(
            r"\b(?:support(?:s|ing)?|soutient|soutenir|stutzt|unterstutzt|"
            r"sostiene|sostener|sostiene|sostenere)\b",
            correction,
        ):
            return None
        joseph = next(
            (p.name for p in self.characters if character_key(p.name) == "joseph"), None
        )
        mary = next(
            (p.name for p in self.characters if character_key(p.name) == "mary"), None
        )
        if joseph and mary:
            return ActionBinding(
                subject=joseph,
                action="supports",
                object=f"{mary} with his arm around her",
            )
        return None

    def render(self) -> str:
        people = "; ".join(
            c.name
            + (" as " + c.role if c.role else "")
            + (
                ", " + ", ".join(c.name + ": " + a for a in c.attributes)
                if c.attributes
                else ""
            )
            for c in self.characters
        )
        subject_constraints = [
            constraint.render()
            for constraint in self.count_constraints
            if constraint.owner or constraint.subject in self.subjects
        ]
        subjects = "; ".join([*self.subjects, *subject_constraints, people])
        rendered = "\n".join(
            filter(
                None,
                (
                    "Subjects: " + subjects if subjects else "",
                    "Action: "
                    + "; ".join(binding.render() for binding in self.action_bindings)
                    if self.action_bindings
                    else "",
                    "Action: " + self.action if self.action else "",
                    "Setting: " + self.setting if self.setting else "",
                    "Other elements: " + ", ".join(self.elements)
                    if self.elements
                    else "",
                    "Style: " + self.style if self.style else "",
                    "Anatomy: each human character has anatomically plausible limbs, at most two hands, each attached to that human character's arms."
                    if any(character.is_human() for character in self.characters)
                    and self.anatomy
                    else "",
                    "Counts: "
                    + "; ".join(
                        constraint.render() for constraint in self.count_constraints
                    )
                    if self.count_constraints
                    else "",
                ),
            )
        )
        return self.render_text(rendered)

    def set_render_intent(self, source: str) -> None:
        text = normalized(source)
        if self._explicitly_requested(
            r"\b(?:icon|icone|byzantin\w*|nativity|nativite|creche|renaissance|painting|peinture|"
            r"drawing|dessin|illustration|cartoon|watercolou?r)\b",
            text,
        ):
            self.artistic_style = True
        elif re.search(r"photo|realis", text):
            self.artistic_style = False
        for symbol, pattern in (
            ("halo", r"\b(?:halos?|aureoles?)\b"),
            ("star", r"\b(?:stars?|etoiles?)\b"),
            ("angel", r"\b(?:angels?|anges?)\b"),
            ("glowing light", r"glowing light|lumiere divine"),
        ):
            if (
                self._explicitly_requested(pattern, text)
                and symbol not in self.requested_iconography
            ):
                self.requested_iconography.append(symbol)
        # Invented decorations must not become positive vision requirements either.
        for person in self.characters:
            person.attributes = [
                cleaned
                for value in person.attributes
                if (cleaned := self._without_iconography(value).strip())
            ]
            person.role = self._without_iconography(person.role)
            person.visual_description = self._without_iconography(
                person.visual_description
            )
        self.elements = [
            cleaned
            for value in self.elements
            if (cleaned := self._without_iconography(value).strip())
        ]
        if (
            self.birth_requested
            and not self.artistic_style
            and re.search(r"nativity|painting|icon|serene|calm", self.style, re.I)
        ):
            self.style = "realistic depiction"

    @staticmethod
    def _explicitly_requested(pattern: str, text: str) -> bool:
        return any(
            not re.search(
                r"\b(?:no|not|without|never|sans|pas|ohne|sin|senza)\s+"
                r"(?:(?:a|an|the|de|religious|calm|nativity)\s+){0,4}$",
                text[: match.start()],
            )
            for match in re.finditer(pattern, text)
        )

    def _without_iconography(self, text: str) -> str:
        for symbol in ("halo", "star", "angel", "glowing light"):
            if symbol not in self.requested_iconography:
                text = re.sub(
                    r"\b(?:(?:a|an|the)\s+)?" + re.escape(symbol) + r"s?\b",
                    "",
                    text,
                    flags=re.I,
                )
        return text

    def render_text(self, text: str) -> str:
        """Only the image model sees visual descriptions; bindings retain names."""
        realistic = not self.artistic_style and (
            self.birth_requested or bool(re.search(r"photo|realis", self.style, re.I))
        )
        if realistic:
            descriptions = {
                "mary": "the young adult woman with dark hair in a linen tunic",
                "jesus": "the newborn male infant"
                if self.birth_requested
                or any(
                    re.search(r"baby|newborn", p.role, re.I)
                    for p in self.characters
                    if character_key(p.name) == "jesus"
                )
                else "the adult man with dark hair and a beard in a linen tunic",
                "joseph": "the bearded adult man in his thirties in a rough wool tunic",
            }
            replacements = {}
            for person in self.characters:
                key = character_key(person.name)
                if (
                    key in descriptions
                    or person.visual_description
                    or person.name[0].isupper()
                ):
                    replacements[person.name] = (
                        (
                            person.visual_description
                            if not (self.birth_requested and person is self._newborn())
                            else ""
                        )
                        or descriptions.get(key)
                        or self._visual_identity(person)
                    )
                    if key in descriptions:
                        descriptions[key] = replacements[person.name]
            # Replace aliases in action text as well as names in subject fields.
            for name, key in (
                ("Virgin Mary", "mary"),
                ("Mary", "mary"),
                ("Jesus", "jesus"),
                ("Joseph", "joseph"),
            ):
                if any(character_key(p.name) == key for p in self.characters):
                    replacements.setdefault(name, descriptions[key])
            if replacements:
                pattern = (
                    r"\b(?:"
                    + "|".join(
                        re.escape(n)
                        for n in sorted(replacements, key=len, reverse=True)
                    )
                    + r")\b"
                )
                lookup = {
                    name.casefold(): value for name, value in replacements.items()
                }
                text = re.sub(
                    pattern, lambda m: lookup[m.group().casefold()], text, flags=re.I
                )
            text = re.sub(
                r"\b(?:Christmas\s+)?(?:religious\s+)?nativity(?:\s+(?:scene|painting))?\b",
                "",
                text,
                flags=re.I,
            )
        text = self._without_iconography(text)
        if (
            realistic
            and self.birth_moment == "delivery"
            and re.search(r"photo|realis", self.style, re.I)
        ):
            text = re.sub(
                r"(?m)^Style: (.*)$", r"Style: documentary photograph, \1", text
            )
        return text

    @staticmethod
    def _visual_identity(person: Character) -> str:
        """Every name replacement keeps the extracted sex and age explicit."""
        description = person.visual_description.strip()
        sex = person.sex
        age = person.age_group
        if not re.search(rf"\b{re.escape(sex)}\b", description, re.I):
            description = f"{age} {sex} {description}".strip()
        elif not re.search(
            r"\b(?:infant|child|young adult|adult|older adult|teen|twent|thirt|fort|elder)\b",
            description,
            re.I,
        ):
            description = f"{age} {description}"
        return "the " + description.removeprefix("the ").removeprefix(
            "a "
        ).removeprefix("an ")

    def retry_details(self, failed: list[str]) -> str:
        details = []
        for key, question in self.checks():
            if key not in failed:
                continue
            if key.startswith("count_"):
                details.append(
                    self.count_constraints[int(key.removeprefix("count_"))].render()
                )
            elif key in {"birth_delivery", "iconography"}:
                details.append(
                    "Visible delivery in progress, wet newborn with attached umbilical cord, mother's physical effort."
                )
            else:
                details.append(self.render_text(question))
        return " ".join(details)

    def checks(self) -> list[tuple[str, str]]:
        checks = []
        for index, person in enumerate(self.characters):
            checks.append(
                (
                    f"character_{index}",
                    f"Is {person.name}, {person.role}, visibly present?",
                )
            )
            if person.attributes:
                checks.append(
                    (
                        f"attribute_{index}",
                        f"Are these attributes visibly bound to {person.name} only: {', '.join(person.attributes)}?",
                    )
                )
            if (
                self.anatomy
                and person.is_human()
                and not self.has_nonstandard_anatomy()
            ):
                checks.append(
                    (
                        f"anatomy_{index}",
                        f"Does {person.name} have plausible anatomy, with at most two hands attached to their own arms and no extra or fused limbs?",
                    )
                )
        adults = [
            person
            for person in self.characters
            if person.age_group in {"young adult", "adult", "older adult"}
        ]
        men = [person for person in adults if person.sex == "man"]
        women = [person for person in adults if person.sex == "woman"]
        if len(men) == len(women) == 1:
            checks.extend(
                (
                    (
                        "adult_man",
                        f"Is there an adult man visibly present who is {men[0].name}?",
                    ),
                    (
                        "adult_women_count",
                        "How many adult women are visible? Answer true only if exactly one adult woman is visible.",
                    ),
                )
            )
        if self.action:
            checks.append(
                ("action", f"Is this action visibly in progress: {self.action}?")
            )
        if self.birth_moment == "post_birth":
            checks.append(
                ("birth_coherence", "Does the mother have a visibly pregnant belly?")
            )
        elif self.birth_moment == "labor":
            checks.append(("birth_coherence", "Is a baby visible?"))
        elif self.birth_moment == "delivery":
            checks.append(
                (
                    "birth_delivery",
                    "Is a birth happening now: is a baby being delivered or just delivered, with the umbilical cord visible?",
                )
            )
            if not self.artistic_style:
                checks.append(
                    (
                        "iconography",
                        "Does the image look like a calm religious nativity painting rather than a real birth?",
                    )
                )
        for index, binding in enumerate(self.action_bindings):
            if (
                binding.subject == "Joseph"
                and binding.action == "holds"
                and binding.object == "Mary's own hand with his hand"
            ):
                question = (
                    "Is the bearded man holding the hand of the adult woman, "
                    "not the baby's hand?"
                )
            elif binding.action == "supports" and "arm around her" in binding.object:
                question = f"Is {binding.subject} visibly supporting {binding.object}?"
            else:
                question = f"Is {binding.render()} visibly happening?"
            checks.append((f"action_binding_{index}", question))
        if self.setting:
            checks.append(("setting", f"Is this setting visible: {self.setting}?"))
        checks.extend(
            (
                f"count_{index}",
                f"Are exactly {constraint.count} {constraint.subject} visibly present? Answer true only if the exact count is {constraint.count}.",
            )
            for index, constraint in enumerate(self.count_constraints)
        )
        checks.extend(
            (f"element_{i}", f"Is {element} visible?")
            for i, element in enumerate(self.elements)
        )
        if self.style:
            if re.search(r"photo|realistic", self.style, re.I):
                checks.append(
                    (
                        "style",
                        "Is this a realistic photograph, not a drawing or illustration?",
                    )
                )
            else:
                checks.append(
                    ("style", f"Does the image use this style: {self.style}?")
                )
        return checks

    def expected_answers(self) -> dict[str, bool]:
        expected = {key: True for key, _ in self.checks()}
        if self.birth_moment in {"post_birth", "labor"}:
            expected["birth_coherence"] = False
        if "iconography" in expected:
            expected["iconography"] = False
        return expected

    def failure_detail(self, key: str) -> str:
        if key.startswith("count_"):
            try:
                return self.count_constraints[
                    int(key.removeprefix("count_"))
                ].disclosure()
            except (IndexError, ValueError):
                pass
        if key.startswith("action_binding_"):
            try:
                return self.action_bindings[int(key.rsplit("_", 1)[1])].render()
            except (IndexError, ValueError):
                pass
        if key == "action":
            return self.action
        for prefix in ("character_", "attribute_", "element_"):
            if key.startswith(prefix):
                try:
                    index = int(key.rsplit("_", 1)[1])
                    if prefix == "element_":
                        return self.elements[index]
                    character = self.characters[index]
                    if prefix == "attribute_":
                        return ", ".join(character.attributes)
                    return character.name
                except (IndexError, ValueError):
                    pass
        if key == "setting":
            return self.setting
        if key == "style":
            return self.style
        return "the requested detail"

    @staticmethod
    def failure_note_eligible(key: str) -> bool:
        """Every requested, verified detail belongs in the user-facing outcome."""
        return True

    def has_nonstandard_anatomy(self) -> bool:
        return any(
            constraint.subject
            in {
                "leg",
                "legs",
                "arm",
                "arms",
                "head",
                "heads",
                "hand",
                "hands",
                "patte",
                "pattes",
                "bras",
                "tete",
                "tetes",
                "main",
                "mains",
            }
            for constraint in self.count_constraints
        )


def retain_anchors(scene: Scene, anchors: Scene) -> None:
    """A formatter may enrich source fields, but cannot silently delete them."""
    keys = {character_key(c.name) for c in scene.characters}
    scene.characters.extend(
        c for c in anchors.characters if character_key(c.name) not in keys
    )
    scene.subjects = list(dict.fromkeys([*scene.subjects, *anchors.subjects]))
    for field in ("action", "setting", "style"):
        required = getattr(anchors, field)
        actual = getattr(scene, field)
        if required and any(
            word not in normalized(actual) for word in normalized(required).split()
        ):
            setattr(scene, field, required)
    scene.elements = list(dict.fromkeys([*scene.elements, *anchors.elements]))
    scene.cohere_birth_moment()


def anchored_scene(source: str) -> Scene:
    """Known multilingual anchors provide a conservative fallback to model parsing."""
    concepts = {c.name for c in extract_constraints(source) if not c.absent}
    identities: dict[
        str,
        tuple[
            Literal["woman", "man", "person"],
            Literal["infant", "child", "young adult", "adult", "older adult"],
        ],
    ] = {
        "Mary": ("woman", "young adult"),
        "Jesus": ("man", "infant"),
        "Joseph": ("man", "adult"),
    }
    characters = [
        Character(
            name=name,
            sex=identities[name][0],
            age_group=identities[name][1],
            role="an adult companion" if name == "Joseph" else "",
        )
        for name in ("Mary", "Jesus", "Joseph")
        if name in concepts or (name == "Mary" and "Virgin Mary" in concepts)
    ]
    scene = Scene(
        characters=characters,
        action="Mary giving birth to Jesus, childbirth visibly in progress"
        if {"giving birth", "Jesus"} <= concepts
        and any(c.name == "Mary" for c in characters)
        else ("childbirth in progress" if "giving birth" in concepts else ""),
        setting=" ".join(n for n in ("Bethlehem", "stable") if n in concepts),
        elements=["animals"] if "animals" in concepts else [],
        style=" ".join(
            n
            for n in ("photorealistic", "photograph", "charcoal", "illustration")
            if n in concepts
        ),
    )
    scene.cohere_birth_moment(source)
    scene.set_render_intent(source)
    return scene
