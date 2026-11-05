# Third-party licenses

Every third-party component of this repository: the text of the Code of Conduct (CC-BY-4.0, attribution in `CODE_OF_CONDUCT.md`), the direct and transitive dependencies in `uv.lock` (runtime, `dev`, `docs` and `plugins` groups), the build-time dependencies of hatchling, the PostgreSQL extension used in CI, the PostgreSQL driver installed only in one CI run (psycopg, LGPL, not in `uv.lock` and not shipped), the MySQL and MariaDB servers that the CI `mysql` job runs as Docker images (GPL, test services only, not shipped), and the GitHub Actions used by its workflows and by `action/action.yml`. One row per component. `certifi` and `pathspec` are MPL-2.0 and were already in the lock file as transitive dependencies of the `dev` group; they are recorded here and are not part of the installed package.

| source | name | license type | license link |
|---|---|---|---|
| https://github.com/fastapi/annotated-doc | annotated-doc | MIT | https://github.com/fastapi/annotated-doc/blob/main/LICENSE |
| https://github.com/annotated-types/annotated-types | annotated-types | MIT | https://github.com/annotated-types/annotated-types/blob/main/LICENSE |
| https://github.com/agronholm/anyio | anyio | MIT | https://github.com/agronholm/anyio/blob/master/LICENSE |
| https://github.com/mypyc/ast_serialize | ast-serialize | MIT | https://github.com/mypyc/ast_serialize/blob/main/LICENSE |
| https://github.com/certifi/python-certifi | certifi (transitive dependency of httpx; copyleft, already in uv.lock; recorded, not modified) | MPL-2.0 | https://github.com/certifi/python-certifi/blob/master/LICENSE |
| https://github.com/tartley/colorama | colorama | BSD-3-Clause | https://github.com/tartley/colorama/blob/master/LICENSE.txt |
| https://github.com/agronholm/exceptiongroup | exceptiongroup | MIT AND PSF-2.0 | https://github.com/agronholm/exceptiongroup/blob/main/LICENSE |
| https://github.com/python-hyper/h11 | h11 | MIT | https://github.com/python-hyper/h11/blob/master/LICENSE.txt |
| https://github.com/encode/httpcore | httpcore | BSD-3-Clause | https://github.com/encode/httpcore/blob/master/LICENSE.md |
| https://github.com/kjd/idna | idna | BSD-3-Clause | https://github.com/kjd/idna/blob/master/LICENSE.md |
| https://github.com/pytest-dev/iniconfig | iniconfig | MIT | https://github.com/pytest-dev/iniconfig/blob/main/LICENSE |
| https://github.com/mypyc/librt | librt | MIT AND PSF-2.0 AND BSD-2-Clause | https://github.com/mypyc/librt/blob/master/LICENSE |
| https://github.com/python/mypy_extensions | mypy-extensions | MIT | https://github.com/python/mypy_extensions/blob/master/LICENSE |
| https://github.com/pypa/packaging | packaging | Apache-2.0 OR BSD-2-Clause | https://github.com/pypa/packaging/blob/main/LICENSE |
| https://github.com/cpburnz/python-pathspec | pathspec (transitive dependency of mypy and of hatchling; copyleft, already in uv.lock; recorded, not modified; version 1.1.1 ships the MPL-2.0 text only) | MPL-2.0 | https://github.com/cpburnz/python-pathspec/blob/master/LICENSE-MPL-2.0 |
| https://github.com/pytest-dev/pluggy | pluggy | MIT | https://github.com/pytest-dev/pluggy/blob/main/LICENSE |
| https://github.com/pydantic/pydantic | pydantic | MIT | https://github.com/pydantic/pydantic/blob/main/LICENSE |
| https://github.com/pydantic/pydantic | pydantic-core | MIT | https://github.com/pydantic/pydantic/blob/main/pydantic-core/LICENSE |
| https://github.com/Kludex/starlette | starlette | BSD-3-Clause | https://github.com/Kludex/starlette/blob/main/LICENSE.md |
| https://github.com/hukkin/tomli | tomli | MIT | https://github.com/hukkin/tomli/blob/master/LICENSE |
| https://github.com/python/typing_extensions | typing-extensions | PSF-2.0 | https://github.com/python/typing_extensions/blob/main/LICENSE |
| https://github.com/pydantic/typing-inspection | typing-inspection | MIT | https://github.com/pydantic/typing-inspection/blob/main/LICENSE |
| https://github.com/pypa/trove-classifiers | trove-classifiers (build-time dependency of hatchling, not in uv.lock) | Apache-2.0 | https://github.com/pypa/trove-classifiers/blob/main/LICENSE |
| https://github.com/python-poetry/tomlkit | tomlkit (build-time dependency of hatchling, not in uv.lock) | MIT | https://github.com/python-poetry/tomlkit/blob/master/LICENSE |
| https://github.com/pytest-dev/pytest | pytest | MIT | https://github.com/pytest-dev/pytest/blob/main/LICENSE |
| https://github.com/sqlalchemy/sqlalchemy | SQLAlchemy | MIT | https://github.com/sqlalchemy/sqlalchemy/blob/main/LICENSE |
| https://github.com/python-greenlet/greenlet | greenlet | MIT AND PSF-2.0 | https://github.com/python-greenlet/greenlet/blob/master/LICENSE |
| https://github.com/omnilib/aiosqlite | aiosqlite | MIT | https://github.com/omnilib/aiosqlite/blob/main/LICENSE |
| https://github.com/PyMySQL/PyMySQL | PyMySQL (dev group, MySQL and MariaDB tests; MIT text verified in the 1.2.3 sdist) | MIT | https://github.com/PyMySQL/PyMySQL/blob/main/LICENSE |
| https://github.com/aio-libs/aiomysql | aiomysql (dev group, async MySQL and MariaDB tests; MIT text verified in the 0.3.2 sdist) | MIT | https://github.com/aio-libs/aiomysql/blob/main/LICENSE |
| https://github.com/fastapi/fastapi | FastAPI | MIT | https://github.com/fastapi/fastapi/blob/master/LICENSE |
| https://github.com/encode/httpx | httpx | BSD-3-Clause | https://github.com/encode/httpx/blob/master/LICENSE.md |
| https://github.com/MagicStack/asyncpg | asyncpg | Apache-2.0 | https://github.com/MagicStack/asyncpg/blob/master/LICENSE |
| https://github.com/aio-libs-abandoned/async-timeout | async-timeout | Apache-2.0 | https://github.com/aio-libs-abandoned/async-timeout/blob/master/LICENSE |
| https://codeberg.org/tlocke/pg8000 | pg8000 | BSD-3-Clause | https://codeberg.org/tlocke/pg8000/src/branch/main/LICENSE |
| https://codeberg.org/tlocke/scramp | scramp | MIT-0 | https://codeberg.org/tlocke/scramp/src/branch/main/LICENSE |
| https://github.com/wbond/asn1crypto | asn1crypto | MIT | https://github.com/wbond/asn1crypto/blob/master/LICENSE |
| https://github.com/dateutil/dateutil | python-dateutil | Apache-2.0 OR BSD-3-Clause | https://github.com/dateutil/dateutil/blob/master/LICENSE |
| https://github.com/benjaminp/six | six | MIT | https://github.com/benjaminp/six/blob/main/LICENSE |
| https://github.com/coveragepy/coveragepy | coverage | Apache-2.0 | https://github.com/coveragepy/coveragepy/blob/main/LICENSE.txt |
| https://github.com/Python-Markdown/markdown | Markdown (docs group, builds the documentation site) | BSD-3-Clause | https://github.com/Python-Markdown/markdown/blob/master/LICENSE.md |
| https://github.com/pygments/pygments | Pygments (docs group, highlights code in the documentation site) | BSD-2-Clause | https://github.com/pygments/pygments/blob/master/LICENSE |
| https://github.com/astral-sh/ruff | ruff | MIT | https://github.com/astral-sh/ruff/blob/main/LICENSE |
| https://github.com/python/mypy | mypy | MIT | https://github.com/python/mypy/blob/master/LICENSE |
| https://github.com/pypa/hatch | hatchling | MIT | https://github.com/pypa/hatch/blob/master/LICENSE.txt |
| https://github.com/pytest-dev/pytest-xdist | pytest-xdist | MIT | https://github.com/pytest-dev/pytest-xdist/blob/master/LICENSE |
| https://github.com/pytest-dev/execnet | execnet | MIT | https://github.com/pytest-dev/execnet/blob/main/LICENSE |
| https://github.com/HypoPG/hypopg | hypopg (PostgreSQL extension, used optionally at runtime through SQL and installed in the CI image; not a Python dependency) | PostgreSQL | https://github.com/HypoPG/hypopg/blob/REL1_STABLE/LICENSE |
| https://github.com/github/codeql-action | github/codeql-action/upload-sarif (GitHub Action, pinned v4.38.2) | MIT | https://github.com/github/codeql-action/blob/main/LICENSE |
| https://github.com/actions/setup-python | actions/setup-python (GitHub Action, pinned v7.0.0) | MIT | https://github.com/actions/setup-python/blob/main/LICENSE |
| https://github.com/actions/checkout | actions/checkout (GitHub Action, pinned v7.0.1) | MIT | https://github.com/actions/checkout/blob/main/LICENSE |
| https://github.com/astral-sh/setup-uv | astral-sh/setup-uv (GitHub Action, pinned v10.3.0) | MIT | https://github.com/astral-sh/setup-uv/blob/main/LICENSE |
| https://github.com/actions/upload-artifact | actions/upload-artifact (GitHub Action, pinned v7.0.2) | MIT | https://github.com/actions/upload-artifact/blob/main/LICENSE |
| https://github.com/actions/download-artifact | actions/download-artifact (GitHub Action, pinned v8.0.2) | MIT | https://github.com/actions/download-artifact/blob/main/LICENSE |
| https://github.com/pypa/gh-action-pypi-publish | pypa/gh-action-pypi-publish (GitHub Action, pinned v1.14.2) | BSD-3-Clause | https://github.com/pypa/gh-action-pypi-publish/blob/release/v1/LICENSE.md |
| https://github.com/actions/configure-pages | actions/configure-pages (GitHub Action, pinned v6.0.0) | MIT | https://github.com/actions/configure-pages/blob/main/LICENSE |
| https://github.com/actions/upload-pages-artifact | actions/upload-pages-artifact (GitHub Action, pinned v5.0.0) | MIT | https://github.com/actions/upload-pages-artifact/blob/main/LICENSE |
| https://github.com/actions/deploy-pages | actions/deploy-pages (GitHub Action, pinned v5.0.1) | MIT | https://github.com/actions/deploy-pages/blob/main/LICENSE |
| https://github.com/FactoryBoy/factory_boy | factory-boy (dev group, fixtures of the accuracy corpus) | MIT | https://github.com/FactoryBoy/factory_boy/blob/master/LICENSE |
| https://github.com/joke2k/faker | faker (transitive dependency of factory-boy) | MIT | https://github.com/joke2k/faker/blob/master/LICENSE.txt |
| https://github.com/python/tzdata | tzdata (transitive dependency of faker, Windows only) | Apache-2.0 | https://github.com/python/tzdata/blob/master/LICENSE |
| https://github.com/pallets-eco/flask-sqlalchemy | flask-sqlalchemy (dev group, Flask-SQLAlchemy scenarios of the accuracy corpus) | BSD-3-Clause | https://github.com/pallets-eco/flask-sqlalchemy/blob/main/LICENSE.rst |
| https://github.com/pallets/flask | flask (transitive dependency of flask-sqlalchemy) | BSD-3-Clause | https://github.com/pallets/flask/blob/main/LICENSE.txt |
| https://github.com/pallets/click | click (transitive dependency of flask) | BSD-3-Clause | https://github.com/pallets/click/blob/main/LICENSE.txt |
| https://github.com/pallets/jinja | jinja2 (transitive dependency of flask) | BSD-3-Clause | https://github.com/pallets/jinja/blob/main/LICENSE.txt |
| https://github.com/pallets/markupsafe | markupsafe (transitive dependency of flask) | BSD-3-Clause | https://github.com/pallets/markupsafe/blob/main/LICENSE.txt |
| https://github.com/pallets/werkzeug | werkzeug (transitive dependency of flask) | BSD-3-Clause | https://github.com/pallets/werkzeug/blob/main/LICENSE.txt |
| https://github.com/pallets/itsdangerous | itsdangerous (transitive dependency of flask) | BSD-3-Clause | https://github.com/pallets/itsdangerous/blob/main/LICENSE.txt |
| https://github.com/pallets-eco/blinker | blinker (transitive dependency of flask) | MIT | https://github.com/pallets-eco/blinker/blob/main/LICENSE.txt |
| https://github.com/fastapi/sqlmodel | sqlmodel (not in uv.lock: it requires SQLAlchemy below 2.1, so only the SQLAlchemy 2.0 CI job installs it; used by the accuracy corpus) | MIT | https://github.com/fastapi/sqlmodel/blob/main/LICENSE |
| https://github.com/EthicalSource/contributor_covenant/blob/b1b0a0f28b68717456ef4f93566356d14730cb82/content/version/2/1/code_of_conduct.md | Contributor Covenant 2.1 (text of CODE_OF_CONDUCT.md, adapted: contact method filled in; the repository's LICENSE at that commit is CC BY 4.0, later releases of the upstream repository changed its license, so the text was taken from this commit) | CC-BY-4.0 | https://creativecommons.org/licenses/by/4.0/legalcode |
| https://github.com/pytest-dev/pytest-timeout | pytest-timeout (dev group, timeout interplay tests; MIT text verified in the 2.4.0 sdist) | MIT | https://github.com/pytest-dev/pytest-timeout/blob/main/LICENSE |
| https://github.com/pytest-dev/pytest-randomly | pytest-randomly (plugins group, interplay tests; MIT text verified in the 5.0.0 sdist) | MIT | https://github.com/pytest-dev/pytest-randomly/blob/main/LICENSE |
| https://github.com/pytest-dev/pytest-asyncio | pytest-asyncio (plugins group, async mode tests; Apache-2.0 text verified in the 1.4.0 sdist) | Apache-2.0 | https://github.com/pytest-dev/pytest-asyncio/blob/main/LICENSE |
| https://github.com/samypr100/backports.asyncio.runner | backports-asyncio-runner (dependency of pytest-asyncio below Python 3.11; PSF-2.0 text verified in the 1.2.0 sdist) | PSF-2.0 | https://github.com/samypr100/backports.asyncio.runner/blob/main/LICENSE.md |
| https://github.com/mysql/mysql-server | MySQL Community Server (official Docker image `mysql:8.4`, run only as a test service in the CI mysql job; not a dependency of the package and not shipped) | GPL-2.0-only | https://github.com/mysql/mysql-server/blob/trunk/LICENSE |
| https://github.com/MariaDB/server | MariaDB Server (official Docker image `mariadb:11.8`, run only as a test service in the CI mysql job; not a dependency of the package and not shipped) | GPL-2.0-only | https://github.com/MariaDB/server/blob/main/COPYING |
| https://github.com/psycopg/psycopg | psycopg (and psycopg-binary; installed only in the psycopg run of the CI postgres job with uv pip, not in uv.lock, not a dependency of the package and not shipped) | LGPL-3.0-only | https://github.com/psycopg/psycopg/blob/master/LICENSE.txt |
