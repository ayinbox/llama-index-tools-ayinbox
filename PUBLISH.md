# Publishing to PyPI

The `Release` workflow (`.github/workflows/release.yml`) builds and publishes `llama-index-tools-ayinbox` when a `v*` tag is pushed. It uses PyPI trusted publishing (OIDC), so no PyPI token is stored anywhere.

## One-time setup

1. PyPI account: sign in to https://pypi.org with the account that will own the `llama-index-tools-ayinbox` project, and turn on two-factor authentication.
2. Pending trusted publisher: the project does not exist yet, so add a pending publisher at https://pypi.org/manage/account/publishing/ with:
   - PyPI project name: `llama-index-tools-ayinbox`
   - Owner: `ayinbox`
   - Repository name: `llama-index-tools-ayinbox`
   - Workflow name: `release.yml`
   - Environment name: `pypi`

   The first successful publish creates the project and turns the pending publisher into a normal one.
3. GitHub environment: in https://github.com/ayinbox/llama-index-tools-ayinbox/settings/environments create an environment named `pypi`. Adding required reviewers there makes every release wait for a person to approve it.
4. Optional: add the other maintainers as owners of the project on PyPI after the first release.

## Releasing

1. Set the version in `llama_index/tools/ayinbox/_version.py` and give the `CHANGELOG.md` entry its release date.
2. Commit, push `main`, and wait for CI to pass.
3. Tag and push the tag:

   ```sh
   git tag v0.1.0
   git push origin v0.1.0
   ```

The workflow checks that the tag matches `__version__`, runs the tests, builds the sdist and wheel, and publishes them through the `pypi` environment.

After the first release, change the README install section to `pip install llama-index-tools-ayinbox`. To be listed on LlamaHub, open a pull request against the LlamaIndex repository once the package is on PyPI.
