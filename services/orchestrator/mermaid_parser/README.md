# Diagram parser runtime

Use Node 22 and npm for local runs. Run `npm ci --ignore-scripts --omit=dev`
at the repository root to install the locked parser. `make test` and
`scripts/serve.sh` install it when absent; the application Docker image includes
Node and the installed packages. Direct unittest invocations need this install first.

Mermaid 11.10.1 is the version floor used by Open WebUI v0.11.3. Using its real
parser avoids accepting diagrams with an incomplete Python grammar. Mermaid is
MIT licensed. jsdom 26.1.0 (MIT) supplies the DOM required by Mermaid's sanitizer;
it runs without scripts or network resources. The lockfile pins the transitive
dependencies. No browser download is needed at runtime.

`check.mjs` accepts a JSON array of diagram sources on stdin and emits only an
array of booleans. The Python caller applies a five-second timeout, a Node heap
limit, cancellation cleanup and a bounded cache. Model text and parser error
messages are never logged. Missing runtime dependencies fail closed with the
same plain drawing-unavailable response as invalid syntax.
