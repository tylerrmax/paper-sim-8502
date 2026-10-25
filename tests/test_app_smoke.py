#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py 烟雾测试（不需安装 Streamlit）。

用假的 streamlit 模块让 app.py 对「临时副本」的 db/sim.db 在每个导航分页整体执行，
抓执行期错误（NameError、SQL 错误、解包错误、None 格式化等）。
- 所有按钮/表单一律返回 False，不会触发管线或写库
- 网络行情一律 stub 为空
注意：这只验证「能跑、不报错、关键内容有输出」，不验证界面外观。
用法： python3 tests/test_app_smoke.py   （或 pytest）
"""
import contextlib
import os
import shutil
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Ctx:
    """既能当 context manager 又能吞任意调用/属性的通用对象。"""
    def __init__(self, rec):
        self._rec = rec

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __getattr__(self, name):
        return _make_fn(self._rec, name)

    def __bool__(self):
        # 列/侧栏对象上的 .button()/.checkbox() 结果必须为 False，否则等于假装按下按钮
        return False

    def __iter__(self):
        return iter(())

    def __call__(self, *a, **k):
        return _Ctx(self._rec)


def _make_fn(rec, name):
    def fn(*a, **k):
        rec.append((name, a, k))
        return _Ctx(rec)
    return fn


def build_fake_st(tab, rec):
    st = types.ModuleType("streamlit")

    class _Secrets(dict):
        def get(self, key=None, default=None):
            return default
    st.secrets = _Secrets()
    class _State(dict):
        """同真实 st.session_state：字典 + 属性两种存取。"""
        def __getattr__(self, k):
            try:
                return self[k]
            except KeyError:
                raise AttributeError(k)

        def __setattr__(self, k, v):
            self[k] = v
    st.session_state = _State()

    def _cols(spec, *a, **k):
        n = spec if isinstance(spec, int) else len(spec)
        return [_Ctx(rec) for _ in range(n)]

    def _tabs(labels, *a, **k):
        return [_Ctx(rec) for _ in labels]

    def _radio(label, options=None, *a, **k):
        opts = list(options or [])
        if label in ("板块", "主导航"):
            return tab
        key = k.get("key")
        if key in st.session_state:
            return st.session_state[key]
        return opts[0] if opts else None

    def _false(*a, **k):
        rec.append(("button", a, k))
        return False

    def _checkbox(label, value=False, *a, **k):
        return value

    def _num(label, value=0, *a, **k):
        return value

    def _text(*a, **k):
        return ""

    def _select(label, options=None, *a, **k):
        o = list(options or [])
        return o[0] if o else None

    def _rerun():
        raise RuntimeError("unexpected st.rerun() during smoke (a button fired?)")

    def _cache_data(*a, **k):
        # 兼容 @st.cache_data 与 @st.cache_data(ttl=..)：测试中不快取
        if len(a) == 1 and callable(a[0]) and not k:
            return a[0]
        return lambda f: f

    def _fragment(*a, **k):
        if len(a) == 1 and callable(a[0]) and not k:
            return a[0]
        return lambda f: f

    st.columns = _cols
    st.tabs = _tabs
    st.radio = _radio
    st.button = _false
    st.form_submit_button = _false
    st.download_button = _false
    st.checkbox = _checkbox
    st.number_input = _num
    st.text_input = _text
    st.selectbox = _select
    st.date_input = lambda *a, **k: __import__("datetime").date.today()
    st.rerun = _rerun
    st.cache_data = _cache_data
    st.cache_resource = _cache_data
    st.fragment = _fragment
    st.sidebar = _Ctx(rec)
    st.sidebar.radio = _radio
    st.sidebar.button = _false
    st.sidebar.columns = _cols
    st.components = types.SimpleNamespace(v1=types.SimpleNamespace(
        html=lambda *a, **k: rec.append(("html", a, k))))

    for name in ["markdown", "write", "caption", "subheader", "header", "title",
                 "info", "warning", "error", "success", "table", "dataframe",
                 "line_chart", "metric", "divider", "code", "image", "set_page_config",
                 "expander", "form", "container", "spinner", "dialog", "popover",
                 "empty", "progress", "toast", "text", "json", "bar_chart",
                 "column_config", "stop", "data_editor", "status"]:
        setattr(st, name, _make_fn(rec, name))
    st.column_config = _Ctx(rec)
    return st


def run_tab(tab):
    tmp = tempfile.mkdtemp(prefix="smoke_")
    try:
        work = os.path.join(tmp, "repo")
        shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        rec = []
        fake = build_fake_st(tab, rec)
        saved = {k: sys.modules.get(k) for k in ("streamlit", "engine", "engine.realtime", "data")}
        sys.modules["streamlit"] = fake
        for k in [m for m in sys.modules if m == "engine" or m.startswith("engine.")]:
            del sys.modules[k]
        sys.path.insert(0, work)
        cwd = os.getcwd()
        os.chdir(work)
        try:
            import engine.realtime as rt
            rt.fetch_all = lambda syms: {}          # 禁网络
            src = open(os.path.join(work, "app.py"), encoding="utf-8").read()
            g = {"__name__": "__main__", "__file__": os.path.join(work, "app.py")}
            exec(compile(src, os.path.join(work, "app.py"), "exec"), g)
        finally:
            os.chdir(cwd)
            sys.path.remove(work)
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
        return rec
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _tabs_in_app():
    import re
    src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    m = (re.search(r'NAV_OPTIONS\s*=\s*(\[[^\]]*\])', src)
         or re.search(r'st\.sidebar\.radio\("板块",\s*(\[[^\]]*\])', src)
         or re.search(r'st\.radio\("板块",\s*(\[[^\]]*\])', src))
    tabs = eval(m.group(1)) if m else []
    if not tabs:                      # 找不到导航 = 测试自身失效，必须报错而不是空跑通过
        raise AssertionError("烟雾测试没找到导航选项，请更新 _tabs_in_app()")
    return tabs


def test_every_tab_renders():
    for tab in _tabs_in_app():
        rec = run_tab(tab)
        assert rec, f"{tab} 没有任何输出"


def test_screen_tab_has_triple_screen():
    rec = run_tab("🔍 选股")
    texts = " ".join(str(a) for _, a, _ in rec)
    assert "三重校验" in texts, "选股页缺少三重校验区块"
    assert "今日无标的通过三重校验" in texts, "无输入时必须明确提示零通过"


def test_trade_tab_no_pending_after_approval_filter():
    # 已审批排除条件存在于 SQL
    src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    assert "ap.date IS NULL" in src


if __name__ == "__main__":
    ok = True
    for tab in _tabs_in_app():
        try:
            rec = run_tab(tab)
            print(f"✅ {tab}: {len(rec)} 次 UI 调用")
        except Exception as e:
            ok = False
            import traceback
            print(f"❌ {tab}: {type(e).__name__}: {e}")
            traceback.print_exc()
    sys.exit(0 if ok else 1)
