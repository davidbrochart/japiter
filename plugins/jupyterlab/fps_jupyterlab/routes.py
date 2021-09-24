import json
from pathlib import Path
import sys
from glob import glob
from http import HTTPStatus
import pkg_resources  # type: ignore
from typing import Optional

from babel import Locale  # type: ignore
import jupyterlab  # type: ignore
import jupyverse  # type: ignore
from fastapi import APIRouter, Response, Depends, status
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request  # type: ignore
from fps.hooks import register_router  # type: ignore

from fps_auth.db import get_user_db  # type: ignore
from fps_auth.backends import (  # type: ignore
    current_user,
    cookie_authentication,
    LoginCookieAuthentication,
    get_user_manager,
)
from fps_auth.models import User  # type: ignore
from fps_auth.config import get_auth_config  # type: ignore

from .config import get_jlab_config

router = APIRouter()
prefix_dir: Path = Path(sys.prefix)
LOCALE = "en"

router.mount(
    "/static/lab",
    StaticFiles(directory=prefix_dir / "share" / "jupyter" / "lab" / "static"),
    name="static",
)

router.mount(
    "/lab/api/themes",
    StaticFiles(directory=prefix_dir / "share" / "jupyter" / "lab" / "themes"),
    name="themes",
)

federated_extensions = []
for path in glob(
    str(prefix_dir / "share" / "jupyter" / "labextensions" / "**" / "package.json"),
    recursive=True,
):
    with open(path) as f:
        package = json.load(f)
    name = package["name"]
    extension = package["jupyterlab"]["_build"]
    extension["name"] = name
    federated_extensions.append(extension)
    router.mount(
        f"/lab/extensions/{name}/static",
        StaticFiles(
            directory=prefix_dir
            / "share"
            / "jupyter"
            / "labextensions"
            / name
            / "static"
        ),
        name=name,
    )


@router.get("/")
async def get_root(
    response: Response,
    token: Optional[str] = None,
    auth_config=Depends(get_auth_config),
    jlab_config=Depends(get_jlab_config),
    user_db=Depends(get_user_db),
    user_manager=Depends(get_user_manager),
):
    if token and auth_config.mode == "token":
        user = await user_db.get(token)
        if user:
            await super(
                LoginCookieAuthentication, cookie_authentication
            ).get_login_response(user, response, user_manager)
    # auto redirect
    response.status_code = status.HTTP_302_FOUND
    response.headers["Location"] = jlab_config.base_url + "lab"


@router.get("/lab")
async def get_lab(
    user: User = Depends(current_user()), jlab_config=Depends(get_jlab_config)
):
    return HTMLResponse(
        get_index("default", jlab_config.collaborative, jlab_config.base_url)
    )


@router.get("/lab/tree/{path:path}")
async def load_workspace(path, jlab_config=Depends(get_jlab_config)):
    return HTMLResponse(
        get_index("default", jlab_config.collaborative, jlab_config.base_url)
    )


@router.get("/favicon.ico")
async def get_favicon():
    return FileResponse(Path(jupyverse.__file__).parent / "static" / "favicon.ico")


@router.get("/static/notebook/components/MathJax/{rest_of_path:path}")
async def get_mathjax(rest_of_path):
    return RedirectResponse(
        "https://cdnjs.cloudflare.com/ajax/libs/mathjax/2.7.7/" + rest_of_path
    )


@router.get("/lab/api/listings/@jupyterlab/extensionmanager-extension/listings.json")
async def get_listings():
    return {
        "blocked_extensions_uris": [],
        "allowed_extensions_uris": [],
        "blocked_extensions": [],
        "allowed_extensions": [],
    }


@router.get("/lab/api/workspaces/{name}")
async def get_workspace_data(user: User = Depends(current_user())):
    if user:
        return json.loads(user.workspace)
    return {}


@router.put(
    "/lab/api/workspaces/{name}",
    status_code=204,
)
async def set_workspace(
    request: Request,
    user: User = Depends(current_user()),
    user_db=Depends(get_user_db),
):
    user.workspace = await request.body()
    await user_db.update(user)
    return Response(status_code=HTTPStatus.NO_CONTENT.value)


@router.get("/lab/workspaces/{name}", response_class=HTMLResponse)
async def get_workspace(
    name, user: User = Depends(current_user()), jlab_config=Depends(get_jlab_config)
):
    return get_index(name, jlab_config.collaborative, jlab_config.base_url)


@router.get("/lab/api/translations")
async def get_translations():
    locale = Locale.parse("en")
    data = {
        "en": {
            "displayName": locale.get_display_name(LOCALE).capitalize(),
            "nativeName": locale.get_display_name().capitalize(),
        }
    }
    for ep in pkg_resources.iter_entry_points(group="jupyterlab.languagepack"):
        locale = Locale.parse(ep.name)
        data[ep.name] = {
            "displayName": locale.get_display_name(LOCALE).capitalize(),
            "nativeName": locale.get_display_name().capitalize(),
        }
    return {"data": data, "message": ""}


@router.get("/lab/api/translations/{language}")
async def get_translation(
    language,
):
    global LOCALE
    if language == "en":
        LOCALE = language
        return {}
    for ep in pkg_resources.iter_entry_points(group="jupyterlab.languagepack"):
        if ep.name == language:
            break
    else:
        return {"data": {}, "message": f"Language pack '{language}' not installed!"}
    LOCALE = language
    package = ep.load()
    data = {}
    for path in (
        Path(package.__file__).parent / "locale" / "fr_FR" / "LC_MESSAGES"
    ).glob("*.json"):
        with open(path) as f:
            data.update({path.stem: json.load(f)})
    return {"data": data, "message": ""}


@router.get("/lab/api/settings/{name0}/{name1}:{name2}")
async def get_setting(
    name0,
    name1,
    name2,
    user: User = Depends(current_user()),
):
    with open(
        prefix_dir / "share" / "jupyter" / "lab" / "static" / "package.json"
    ) as f:
        package = json.load(f)
    if name0 == "@jupyterlab":
        lab_or_extensions = Path("lab")
    else:
        lab_or_extensions = Path("labextensions") / name0 / name1
    with open(
        prefix_dir
        / "share"
        / "jupyter"
        / lab_or_extensions
        / "schemas"
        / name0
        / name1
        / f"{name2}.json"
    ) as f:
        schema = json.load(f)
    key = f"{name1}:{name2}"
    result = {
        "id": f"@jupyterlab/{key}",
        "schema": schema,
        "version": package["version"],
        "raw": "{}",
        "settings": {},
        "last_modified": None,
        "created": None,
    }
    if user:
        settings = json.loads(user.settings)
        if key in settings:
            result.update(settings[key])
    return result


@router.put(
    "/lab/api/settings/@jupyterlab/{name0}:{name1}",
    status_code=204,
)
async def change_setting(
    request: Request,
    name0,
    name1,
    user: User = Depends(current_user()),
    user_db=Depends(get_user_db),
):
    settings = json.loads(user.settings)
    settings[f"{name0}:{name1}"] = await request.json()
    user.settings = json.dumps(settings)
    await user_db.update(user)
    return Response(status_code=HTTPStatus.NO_CONTENT.value)


@router.get("/lab/api/settings")
async def get_settings(user: User = Depends(current_user())):
    with open(
        prefix_dir / "share" / "jupyter" / "lab" / "static" / "package.json"
    ) as f:
        package = json.load(f)
    if user:
        user_settings = json.loads(user.settings)
    else:
        user_settings = {}
    settings = []
    for path in (
        prefix_dir / "share" / "jupyter" / "lab" / "schemas" / "@jupyterlab"
    ).glob("*/*.json"):
        with open(path) as f:
            schema = json.load(f)
        key = f"{path.parent.name}:{path.stem}"
        setting = {
            "id": f"@jupyterlab/{key}",
            "schema": schema,
            "version": package["version"],
            "raw": "{}",
            "settings": {},
            "warning": None,
            "last_modified": None,
            "created": None,
        }
        if key in user_settings:
            setting.update(user_settings[key])
        settings.append(setting)
    return {"settings": settings}


INDEX_HTML = """\
<!doctype html><html lang="en"><head><meta charset="utf-8"><title>JupyterLab</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<script id="jupyter-config-data" type="application/json">PAGE_CONFIG</script>
<script defer="defer" src="FULL_STATIC_URL/main.MAIN_ID.js?v=MAIN_ID"
></script></head><body><script>/* Remove token from URL. */
  (function () {
    var location = window.location;
    var search = location.search;

    // If there is no query string, bail.
    if (search.length <= 1) {
      return;
    }

    // Rebuild the query string without the `token`.
    var query = '?' + search.slice(1).split('&')
      .filter(function (param) { return param.split('=')[0] !== 'token'; })
      .join('&');

    // Rebuild the URL with the new query string.
    var url = location.origin + location.pathname +
      (query !== '?' ? query : '') + location.hash;

    if (url === location.href) {
      return;
    }

    window.history.replaceState({ }, '', url);
  })();</script></body></html>
"""


def get_index(workspace, collaborative, base_url="/"):
    for path in (prefix_dir / "share" / "jupyter" / "lab" / "static").glob("main.*.js"):
        main_id = path.name.split(".")[1]
        break
    full_static_url = f"{base_url}static/lab"
    page_config = {
        "appName": "JupyterLab",
        "appNamespace": "lab",
        "appUrl": "/lab",
        "appVersion": jupyterlab.__version__,
        "baseUrl": base_url,
        "cacheFiles": False,
        "collaborative": collaborative,
        "disabledExtensions": [],
        "exposeAppInBrowser": False,
        "extraLabextensionsPath": [],
        "federated_extensions": federated_extensions,
        "fullAppUrl": f"{base_url}lab",
        "fullLabextensionsUrl": f"{base_url}lab/extensions",
        "fullLicensesUrl": f"{base_url}lab/api/licenses",
        "fullListingsUrl": f"{base_url}lab/api/listings",
        "fullMathjaxUrl": f"{base_url}static/notebook/components/MathJax/MathJax.js",
        "fullSettingsUrl": f"{base_url}lab/api/settings",
        "fullStaticUrl": full_static_url,
        "fullThemesUrl": f"{base_url}lab/api/themes",
        "fullTranslationsApiUrl": f"{base_url}lab/api/translations",
        "fullTreeUrl": f"{base_url}lab/tree",
        "fullWorkspacesApiUrl": f"{base_url}lab/api/workspaces",
        "ignorePlugins": [],
        "labextensionsUrl": "/lab/extensions",
        "licensesUrl": "/lab/api/licenses",
        "listingsUrl": "/lab/api/listings",
        "mathjaxConfig": "TeX-AMS-MML_HTMLorMML-full,Safe",
        "mode": "multiple-document",
        "notebookVersion": "[1, 9, 0]",
        "quitButton": True,
        "settingsUrl": "/lab/api/settings",
        "store_id": 0,
        "schemasDir": str(prefix_dir / "share" / "jupyter" / "lab" / "schemas"),
        "terminalsAvailable": True,
        "themesDir": str(prefix_dir / "share" / "jupyter" / "lab" / "themes"),
        "themesUrl": "/lab/api/themes",
        "token": "4e2804532de366abc81e32ab0c6bf68a73716fafbdbb2098",
        "translationsApiUrl": "/lab/api/translations",
        "treePath": "",
        "workspace": workspace,
        "treeUrl": "/lab/tree",
        "workspacesApiUrl": "/lab/api/workspaces",
        "wsUrl": "",
    }
    index = (
        INDEX_HTML.replace("PAGE_CONFIG", json.dumps(page_config))
        .replace("FULL_STATIC_URL", full_static_url)
        .replace("MAIN_ID", main_id)
    )
    return index


r = register_router(router)
