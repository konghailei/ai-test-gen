"""配置加载的健壮性测试（v4.1.7）。

## 为什么需要这个文件

`config.py` 的 `Settings` 是所有模块的导入前提——它一崩，整个应用连
`import app.main` 都过不去。而这层保护此前**完全依赖"没人往环境里塞空串"**
这个假设，一旦破掉，症状是"部署后容器无限重启，日志只有 pydantic 报错"。

## 背景（真实事故）

v4.1.7 给 `docker-compose.yml` 加了一项可选配置：

```yaml
GEN_TASK_CONCURRENCY_MAX: ${GEN_TASK_CONCURRENCY_MAX:-}
```

`:-` 的语义是"未设置时用空串"，于是 **只要用户没在 .env 里显式写这一项**，
容器里就会存在 `GEN_TASK_CONCURRENCY_MAX=""`。而 `Settings` 里它是 `int`：

    pydantic_core.ValidationError: Input should be a valid integer,
    unable to parse string as an integer [input_value='']

→ **应用启动即失败**。加一个"可选配置"反而让原本能跑的部署全崩，
且报错里只有字段名，看不出是 compose 注入空串导致的。

修复是在 `SettingsConfigDict` 上设 `env_ignore_empty=True`（空串视为未设置）。
本文件锁住这个行为：**删掉那一行，这里必红**。

## 断言口径

只断言"能加载出合法值"，不断言具体数值——因为具体值受代码默认值影响，
而这里要防的是"空串导致解析失败"这一类问题，不是某个配置的取值。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))  # 支持 python tests/xxx.py 直跑


def _reload_settings(monkeypatch, **env):
    """在给定环境变量下重新构造 Settings（不污染全局 settings 单例）。

    为什么不用 `importlib.reload(app.config)`：那会重建整个 settings 单例，
    其他已导入模块持有的引用会指向旧对象，导致同一测试进程里出现两个配置源
    （典型的"测试间互相污染"）。这里直接构造一个新实例。
    """
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from app.config import Settings

    return Settings()


def test_empty_int_env_falls_back_to_default(monkeypatch):
    """**核心回归**：int 类型配置收到空串时必须回退默认值，而不是抛异常。

    对应真实场景：compose 写 `${VAR:-}` + .env 未设置该变量。
    """
    s = _reload_settings(monkeypatch, GEN_TASK_CONCURRENCY_MAX="")
    assert isinstance(s.GEN_TASK_CONCURRENCY_MAX, int), "空串必须被忽略并回退 int 默认值"
    assert s.GEN_TASK_CONCURRENCY_MAX >= 1


def test_empty_float_env_falls_back_to_default(monkeypatch):
    """float 类型同理：`MEM_LOW_RATIO: ${...:-}` 也会注入空串。"""
    s = _reload_settings(monkeypatch, MEM_LOW_RATIO="")
    assert isinstance(s.MEM_LOW_RATIO, float)
    assert 0 < s.MEM_LOW_RATIO < 1


def test_empty_str_env_still_means_empty(monkeypatch):
    """str 类型不受影响：空串本来就合法，且 `""` 在业务上常表示"关闭/未启用"。

    例如 `DEDUP_EMBED_WORKERS=""` = 串行，这是**有效语义**而非"未设置"。
    若哪天改成 `env_ignore_empty` 之外的做法（如统一 strip 后转 None），
    这条会先红，提醒别把 str 的语义一起改坏。
    """
    s = _reload_settings(monkeypatch, DEDUP_EMBED_WORKERS="")
    assert s.DEDUP_EMBED_WORKERS == ""


def test_valid_int_env_is_respected(monkeypatch):
    """非空值必须照常生效——防止修复空串时把正常覆盖也一起吞掉。"""
    s = _reload_settings(monkeypatch, GEN_TASK_CONCURRENCY_MAX="10")
    assert s.GEN_TASK_CONCURRENCY_MAX == 10


def test_garbage_int_env_still_raises(monkeypatch):
    """**非法值必须仍然报错**，不能被"容错"成默认值。

    这条是防止过度修复：如果把 env_ignore_empty 误改成"任何解析失败都退默认"，
    那么把 `GEN_TASK_CONCURRENCY_MAX=abc` 这种配置错误会被静默忽略，
    运维以为设了 8 其实一直跑的是 4 —— 属于"静默失效"，比崩溃更危险。
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _reload_settings(monkeypatch, GEN_TASK_CONCURRENCY_MAX="abc")


if __name__ == "__main__":
    # 支持 python tests/test_config_env_empty.py 直跑（与其它脚本式测试一致）
    sys.exit(pytest.main([__file__, "-q"]))
