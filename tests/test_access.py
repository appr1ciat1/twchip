"""網站保護：Access 檢查的判斷規則、佔位頁、以及部署流程（pages.yml）的安全順序。"""
from pathlib import Path

import pytest

from twchip import access, cli
from twchip.site import placeholder

ROOT = Path(__file__).resolve().parents[1]
LOGIN = "https://myteam.cloudflareaccess.com/cdn-cgi/access/login/twchip.pages.dev?kid=x"


@pytest.mark.parametrize("status,loc,want", [
    (302, LOGIN, True),                                            # Access 登入頁
    (303, LOGIN, True),
    (401, "", True),
    (403, "", True),
    (200, "", False),                                              # 未登入就拿得到內容
    (404, "", False),                                              # 不能當成受保護：可能是網址錯了
    (302, "https://twchip.pages.dev/index.html", False),           # 轉址到別處
    (302, "https://cloudflareaccess.com.evil.example/login", False),  # 網域只是開頭相同
    (302, "https://evilcloudflareaccess.com/login", False),        # 網域只是結尾相同、少了點
    (302, "", False),
    (None, "", False),                                             # 連線失敗：無法確認
])
def test_judge(status, loc, want):
    assert access.judge(status, loc)[0] is want


class R:
    def __init__(self, status, loc=""):
        self.status_code, self.headers = status, {"Location": loc} if loc else {}


def test_check_fails_closed_on_network_error():
    def boom(url):
        raise ConnectionError("down")
    assert access.check("https://x.pages.dev/", boom)[0] is False


def test_check_all_requires_every_url(capsys):
    resp = {"https://a.pages.dev/": R(302, LOGIN), "https://b.pages.dev/": R(200)}
    assert access.check_all(list(resp), lambda u: resp[u]) is False
    assert "未受保護 https://b.pages.dev/" in capsys.readouterr().out
    assert access.check_all(["https://a.pages.dev/"], lambda u: resp[u]) is True


def test_cli_exit_code(monkeypatch):
    monkeypatch.setattr(access.requests, "get", lambda u, **k: R(302, LOGIN) if "safe" in u else R(200))
    assert cli.main(["access-check", "https://safe.pages.dev/"]) == 0
    assert cli.main(["access-check", "https://safe.pages.dev/", "https://open.pages.dev/"]) == 1


def test_placeholder_has_no_data(tmp_path):
    out = tmp_path / "ph"
    placeholder(out)
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert files == ["_headers", "index.html", "robots.txt"]
    assert "noindex" in (out / "_headers").read_text()
    assert (out / "robots.txt").read_text() == "User-agent: *\nDisallow: /\n"


# ---------------------------------------------------------------- 部署流程
def _wf(name):
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def test_no_artifacts_except_probe():
    """公開 repo 的 artifact 任何 GitHub 使用者都能下載，所以網站與資料都不能變成 artifact。"""
    for p in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        uses = [s.get("uses", "") for j in _wf(p.name)["jobs"].values() for s in j.get("steps", [])]
        bad = [u for u in uses if "upload-artifact" in u or "upload-pages-artifact" in u or "deploy-pages" in u]
        if p.name == "probe.yml":   # probe 的原始回應 artifact 暫時保留（使用者決定）
            continue
        assert not bad, (p.name, bad)


def test_pages_deploys_data_only_after_access_check():
    wf = _wf("pages.yml")
    assert wf["permissions"] == {"contents": "read"}
    assert wf["defaults"]["run"]["shell"] == "bash"           # 開啟 pipefail
    steps = wf["jobs"]["deploy"]["steps"]
    idx = {s.get("id") or s.get("name", ""): i for i, s in enumerate(steps)}
    runs = [s.get("run", "") for s in steps]
    build_i = next(i for i, r in enumerate(runs) if "twchip site --out site" in r)
    check = steps[idx["check"]]
    assert idx["check"] < build_i
    assert "twchip access-check" in check["run"]
    # 開放後（LIVE = yes），保護檢查失敗必須中斷流程，不能 continue-on-error
    assert check["continue-on-error"] == "${{ vars.TWCHIP_SITE_LIVE != 'yes' }}"
    assert steps[build_i]["if"] == "vars.TWCHIP_SITE_LIVE == 'yes'"
    # 佔位頁先部署到預覽分支 guard，正式網址的部署在檢查之後
    assert "--branch=guard" in steps[idx["guard"]]["run"] and idx["guard"] < idx["check"]
    main_deploys = [i for i, r in enumerate(runs) if "--branch=main" in r and "deploy" in r]
    assert main_deploys and min(main_deploys) > idx["check"]
    # 檢查失敗時改回佔位頁
    rb = next(s for s in steps if "steps.check.outcome == 'failure'" in str(s.get("if", "")))
    assert "deploy placeholder" in rb["run"] and "--branch=main" in rb["run"]
