"""俄罗斯钓鱼4（RF4）游戏内时间换算 + 游戏时间提醒。

规则：游戏内 24 小时 = 现实 60 分钟，即 1 游戏小时 = 现实 2.5 分钟，
1 游戏分钟 = 现实 2.5 秒。

用法：
  /校时 08:00 14:30:41
                     —— 重要提醒：只当偏差无法校正时使用
                        校准锚点 = 游戏时间 + 现实时间（精确到秒）。
                        用法：盯着游戏时钟，等它跳到 08:00 的那一瞬间，
                        看现实时钟读到几秒（如 14:30:41），把两个时间一起发过来
                        内置基准：游戏内时间 20:29:00 = 现实时间 2026-10-29 16:51:13
  /校时 17:42        —— 只发游戏时间会有很大误差：没有现实时间，只能拿收到消息那一刻当锚点
  /钓鱼时间          —— 查现在游戏内几点、离天亮或天黑还有多久
  /提醒 18:00 打窝   —— 游戏内到 18:00，现实里主动提醒你（18:00 也可以写 1800）
  /提醒列表          —— 看还挂着哪些提醒
  /取消提醒 2        —— 取消编号 2 的提醒
  LLM 工具 rf4_game_time / rf4_set_reminder —— 聊天里我自己调
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time

from astrbot.api.event import MessageChain, filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

SEC_PER_GAME_MIN = 2.5  # 1 游戏分钟 = 现实 2.5 秒
GAME_MIN_PER_DAY = 1440
LATE_TOLERANCE = 180  # 错过超过 3 分钟的提醒不再补发
MAX_TRIES = 3  # 单条提醒发送最多试 3 次
TICK_SECONDS = 2  # 后台巡检间隔

PLUGIN_DATA_DIR = os.path.join(
    get_astrbot_plugin_data_path(), "astrbot_plugin_rf4_time"
)
ANCHOR_FILE = os.path.join(PLUGIN_DATA_DIR, "anchor.json")
REMINDER_FILE = os.path.join(PLUGIN_DATA_DIR, "reminders.json")

# 内置基准锚点：游戏内 20:29:00 = 现实 2026-10-29 16:51:13（+08:00）
# 让新装用户不校准也能直接用；有偏差再用 /校时 或 /补偿 修正。
DEFAULT_ANCHOR_REAL = 1793263873.0
DEFAULT_ANCHOR_GAME_MIN = 1229.0

MILESTONES = ((6, "天亮"), (21, "天黑"))

# 游戏时间偏差补偿（分钟，可正可负）：在算出来的游戏时间上再加这么多分钟。
# 由插件配置 offset_minutes 决定，插件加载/重载时生效。
OFFSET_MIN = 0.0


def set_offset(minutes) -> float:
    """设置偏差补偿分钟数，非法值按 0 处理，返回实际生效值。"""
    global OFFSET_MIN
    try:
        OFFSET_MIN = float(minutes or 0)
    except (TypeError, ValueError):
        OFFSET_MIN = 0.0
    return OFFSET_MIN

_TIME_RE = re.compile(r"(\d{1,2})\s*[:：.．]\s*(\d{1,2})")
_CLOCK_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[:：.．]\s*(\d{1,2})(?:\s*[:：.．]\s*(\d{1,2}))?")
_PACKED_RE = re.compile(r"(?<!\d)(\d{3,4})(?!\d)")
_CMD_RE = re.compile(r"^[/／]?\s*(打窝提醒|游戏提醒|rf4提醒|提醒)\s*")


def _find_time(text: str) -> tuple[int | None, int, int]:
    """在文本里找第一个时间，返回 (当天第几分钟, 起, 止)。"""
    s = (text or "").strip()
    if not s:
        return None, 0, 0
    m = _TIME_RE.search(s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
    else:
        m = _PACKED_RE.search(s)
        if not m:
            return None, 0, 0
        digits = m.group(1)
        hh, mm = int(digits[:-2]), int(digits[-2:])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None, 0, 0
    return hh * 60 + mm, m.start(), m.end()


def _parse_hhmm(text: str) -> int | None:
    """把 17:42 / 17：42 / 1742 里的时间解析成当天第几分钟。"""
    return _find_time(text)[0]


def parse_offset_arg(text: str) -> int | None:
    """从 /补偿 的参数里抠出分钟数；没带数字返回 None（表示查询当前值）。"""
    s = (text or "").strip()
    s = re.sub(r"^[/／]\s*(偏差补偿|时间补偿|rf4补偿|补偿)", "", s, flags=re.IGNORECASE)
    s = s.replace("＋", "+").replace("－", "-").replace("−", "-").replace("—", "-")
    m = re.search(r"[+-]?\s*\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return int(round(float(m.group(0).replace(" ", ""))))
    except ValueError:
        return None


def _find_real_clock(text: str, start: int = 0,
                     now: float | None = None) -> tuple[float | None, int, int]:
    """在校准文本里找第二个时间「时:分[:秒]」，作为现实时间戳。

    以本地「今天 0 点」为基准拼出时间戳；若算出比现在还晚（超过 30 秒），
    只可能是昨天的时刻（跨天校准），减一天处理。
    """
    now = time.time() if now is None else now
    m = _CLOCK_RE.search(text or "", start)
    if not m:
        return None, 0, 0
    hh, mm = int(m.group(1)), int(m.group(2))
    ss = int(m.group(3)) if m.group(3) else 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        return None, 0, 0
    lt = time.localtime(now)
    midnight = now - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
    ts = midnight + hh * 3600 + mm * 60 + ss
    if ts > now + 30:
        ts -= 86400
    return ts, m.start(), m.end()



def _fmt_game(minutes: float) -> str:
    total = int(round(minutes)) % GAME_MIN_PER_DAY
    return f"{total // 60:02d}:{total % 60:02d}"


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec} 秒" if sec else f"{minutes} 分"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def game_minutes_now(anchor_real: float, anchor_game: float, now: float | None = None) -> float:
    now = time.time() if now is None else now
    elapsed = max(0.0, now - anchor_real)
    return (anchor_game + elapsed / SEC_PER_GAME_MIN + OFFSET_MIN) % GAME_MIN_PER_DAY


def next_milestone(game_minutes: float) -> tuple[str, int, float]:
    """返回 (名称, 还有多少游戏分钟, 还有多少现实秒)。"""
    best_label, best_target = "", 0
    best_delta = float(GAME_MIN_PER_DAY + 1)
    for hour, label in MILESTONES:
        target = int(hour * 60) % GAME_MIN_PER_DAY
        delta = (target - game_minutes) % GAME_MIN_PER_DAY
        if delta < 1e-6:
            delta = float(GAME_MIN_PER_DAY)
        if delta < best_delta:
            best_label, best_target, best_delta = label, target, delta
    return best_label, best_target, best_delta * SEC_PER_GAME_MIN


def remind_real_time(target_game_min: int, anchor_real: float, anchor_game: float,
                     now: float | None = None) -> float:
    """算出「下一次游戏内到 target_game_min」对应现实时间戳。"""
    now = time.time() if now is None else now
    gm = game_minutes_now(anchor_real, anchor_game, now)
    delta = (target_game_min - gm) % GAME_MIN_PER_DAY
    if delta < 1e-6:
        delta = float(GAME_MIN_PER_DAY)
    return now + delta * SEC_PER_GAME_MIN


def format_report(anchor_real: float, anchor_game: float, now: float | None = None) -> str:
    now = time.time() if now is None else now
    gm = game_minutes_now(anchor_real, anchor_game, now)
    label, gtarget, rdelta = next_milestone(gm)
    real_clock = time.strftime("%H:%M:%S", time.localtime(now))
    offset_note = ""
    if abs(OFFSET_MIN) > 1e-9:
        sign = "+" if OFFSET_MIN > 0 else "-"
        offset_note = f"\n已含偏差补偿 {sign}{abs(OFFSET_MIN):g} 游戏分钟（插件配置里改）"
    return (
        f"游戏内 {_fmt_game(gm)}（现实 {real_clock}）"
        f"{offset_note}\n"
        f"祝彦祖亦菲上星上篮！！"
    )


def load_anchor() -> dict:
    """读锚点文件；读不到就返回空 dict。"""
    try:
        with open(ANCHOR_FILE, encoding="utf-8") as f:
            data = json.load(f)
        float(data["anchor_real"]), float(data["anchor_game_min"])
        return data
    except Exception:  # noqa: BLE001
        return {}


def save_anchor(anchor_real: float, anchor_game_min: float) -> dict:
    data = {
        "anchor_real": anchor_real,
        "anchor_game_min": anchor_game_min,
        "real_clock": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(anchor_real)),
        "game_clock": _fmt_game(anchor_game_min),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
    }
    os.makedirs(PLUGIN_DATA_DIR, exist_ok=True)
    tmp = ANCHOR_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ANCHOR_FILE)
    return data


def load_reminders() -> list[dict]:
    try:
        with open(REMINDER_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def save_reminders(items: list[dict]) -> None:
    os.makedirs(PLUGIN_DATA_DIR, exist_ok=True)
    tmp = REMINDER_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, REMINDER_FILE)


def fallback_anchor(now: float | None = None) -> tuple[float, float]:
    """没校准过时用内置基准锚点，保证开箱即用。

    内置基准：游戏内 20:29:00 = 现实 2026-10-29 16:51:13（+08:00）。
    游戏时间与现实时间比例固定（1 游戏分钟 = 2.5 现实秒），
    所以只要比例不变，这个基准长期有效；有偏差用 /校时 或 /补偿 修。
    """
    return DEFAULT_ANCHOR_REAL, DEFAULT_ANCHOR_GAME_MIN


class RF4Time(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        # 这版 AstrBot 的 Star 基类不会把 config 存成 self.config，自己留一份
        self.config = config
        self._watcher: asyncio.Task | None = None
        # 当前是否在用内置基准（没校准过时为 True），用于在输出里提示
        self._using_default = False
        self._apply_config()

    def _apply_config(self) -> None:
        """从插件配置读偏差补偿（分钟，可正可负）。"""
        cfg = self.config if isinstance(self.config, dict) else {}
        raw = cfg.get("offset_minutes", 0)
        self._offset = set_offset(raw)
        if abs(self._offset) > 1e-9:
            self.logger.info(f"rf4 游戏时间偏差补偿：{self._offset:+g} 分钟")

    async def initialize(self) -> None:
        self._apply_config()
        if self._watcher is None or self._watcher.done():
            self._watcher = asyncio.create_task(self._watch_loop())
        pending = [x for x in load_reminders() if not x.get("done")]
        if pending:
            self.logger.info(f"rf4 提醒：恢复 {len(pending)} 条未触发提醒")

    async def terminate(self) -> None:
        if self._watcher and not self._watcher.done():
            self._watcher.cancel()
            try:
                await self._watcher
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._watcher = None

    # ---------- 时间 ----------

    async def _anchor(self) -> tuple[float, float]:
        data = load_anchor()
        self._using_default = not bool(data)
        if not data:
            return fallback_anchor()
        return float(data["anchor_real"]), float(data["anchor_game_min"])

    async def _report(self, with_hint: bool = True) -> str:
        real, game = await self._anchor()
        text = format_report(real, game)
        if with_hint and self._using_default:
            text += (
                "\n（当前用的是内置基准，若和你看到的游戏时间有偏差，"
                "用 /校时 重新校准，或 /补偿 微调）"
            )
        return text

    @filter.command("钓鱼时间", alias={"r4", "rf4", "游戏时间"})
    async def rf4_time_cmd(self, event: AstrMessageEvent):
        yield event.plain_result(await self._report())

    # ---------- 偏差补偿 ----------

    @filter.command("补偿", alias={"偏差补偿", "时间补偿", "rf4补偿"})
    async def offset_cmd(self, event: AstrMessageEvent):
        val = parse_offset_arg(event.message_str or "")
        if val is None:
            cur = OFFSET_MIN
            sign = f"{cur:+g}" if abs(cur) > 1e-9 else "0"
            yield event.plain_result(
                f"当前游戏时间补偿 {sign} 分钟\n"
                "改：/补偿 5 往后推、/补偿 -5 往前拨、/补偿 0 清零\n"
                "这个值也同步写进插件配置 offset_minutes，重载不丢"
            )
            return
        val = max(-1440, min(1440, val))
        self._offset = set_offset(val)
        persisted = self._persist_offset(val)
        real, game = await self._anchor()
        now_gm = game_minutes_now(real, game)
        moved = self._reschedule(now_gm)
        lines = [
            f"补偿调成 {val:+d} 分钟，现在游戏内 {_fmt_game(now_gm)}"
            f"（现实 {time.strftime('%H:%M:%S')}）"
        ]
        if moved:
            lines.append(f"顺手把 {moved} 条还挂着的提醒按新时间重算了")
        if not persisted:
            lines.append("配置文件没写成，这次只在内存里生效，重载后会回到配置里的值")
        yield event.plain_result("\n".join(lines))

    def _persist_offset(self, minutes: int) -> bool:
        """把补偿写回插件配置，成功返回 True。"""
        cfg = self.config
        saver = getattr(cfg, "save_config", None)
        if cfg is None or not callable(saver):
            return False
        try:
            cfg["offset_minutes"] = minutes
            saver()
            return True
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"rf4 补偿写入配置失败：{exc}")
            return False

    @filter.command("校时", alias={"校准时间", "rf4校时"})
    async def calibrate_cmd(self, event: AstrMessageEvent):
        raw = (event.message_str or "").strip()
        gm, _, gm_end = _find_time(raw)
        if gm is None:
            yield event.plain_result(
                "重要提醒：只当偏差无法校正时使用\n"
                "校准锚点 = 游戏时间 + 现实时间（精确到秒）\n"
                "用法：盯着游戏时钟，等它跳到 08:00 的那一瞬间，\n"
                "看现实时钟读到几秒（如 14:30:41），把两个时间一起发过来\n"
                "格式：/校时 08:00 14:30:41\n"
                "只发游戏时间会有很大误差\n"
                "内置基准：游戏内时间 20:29:00 = 现实时间 2026-10-29 16:51:13"
            )
            return
        now = time.time()
        real_ts, _, _ = _find_real_clock(raw, gm_end, now)
        manual = real_ts is not None
        if not manual:
            real_ts = now
        data = save_anchor(real_ts, float(gm))
        moved = self._reschedule(float(gm))
        tail = f"\n顺手把还挂着的 {moved} 条提醒按新时间重算了" if moved else ""
        how = "你给的现实时间" if manual else "收到消息那一刻（没给现实时间，误差几秒）"
        yield event.plain_result(
            f"校准完成，锚点：游戏内 {_fmt_game(gm)} = 现实 {data['real_clock']}"
            f"（按{how}）\n"
            + await self._report()
            + tail
        )

    # ---------- 提醒 ----------

    @staticmethod
    def _reschedule(new_gm: float) -> int:
        """校准后按新锚点重算所有未触发的提醒。"""
        now = time.time()
        items = load_reminders()
        moved = 0
        for it in items:
            if it.get("done"):
                continue
            it["target_real"] = remind_real_time(
                int(it["target_game_min"]), now, float(new_gm), now
            )
            it["real_clock"] = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(it["target_real"])
            )
            moved += 1
        if moved:
            save_reminders(items)
        return moved

    @filter.command("提醒", alias={"打窝提醒", "游戏提醒", "rf4提醒"})
    async def add_reminder_cmd(self, event: AstrMessageEvent):
        raw = event.message_str or ""
        gm, start, end = _find_time(raw)
        if gm is None:
            yield event.plain_result("这么说：/提醒 18:00 打窝，时间可以是 18:00 或 1800")
            return
        text = _CMD_RE.sub("", (raw[:start] + " " + raw[end:]).strip()).strip(" ，,、")
        text = re.sub(r"^(记得|提醒我|提醒|叫我|到点叫我去|到点)", "", text).strip(" ，,、")
        text = text or "到点了"
        await self._add_reminder(event, gm, text)
        real, game = await self._anchor()
        fire_at = remind_real_time(gm, real, game)
        yield event.plain_result(
            f"记下了：游戏内 {_fmt_game(gm)} → {text}\n"
            f"现实 {time.strftime('%H:%M:%S', time.localtime(fire_at))} 提醒你（约 {_fmt_duration(fire_at - time.time())}后）"
        )

    @filter.command("提醒列表", alias={"看提醒", "rf4提醒列表"})
    async def list_reminder_cmd(self, event: AstrMessageEvent):
        items = [x for x in load_reminders() if not x.get("done")]
        if not items:
            yield event.plain_result("没有挂着的提醒")
            return
        now = time.time()
        lines = ["挂着的提醒："]
        for it in sorted(items, key=lambda x: x.get("target_real", 0)):
            left = _fmt_duration(it.get("target_real", now) - now)
            lines.append(
                f"#{it.get('id')} 游戏 {_fmt_game(it.get('target_game_min', 0))} "
                f"{it.get('text', '')}（现实 {left}后）"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("取消提醒", alias={"删提醒"})
    async def del_reminder_cmd(self, event: AstrMessageEvent):
        raw = (event.message_str or "").strip()
        m = re.search(r"\d+", raw)
        if not m:
            yield event.plain_result("要取消哪条？发 /提醒列表 看编号，然后 /取消提醒 2")
            return
        rid = int(m.group())
        items = load_reminders()
        left = [x for x in items if int(x.get("id", -1)) != rid]
        if len(left) == len(items):
            yield event.plain_result(f"没有 #{rid} 这条")
            return
        save_reminders(left)
        yield event.plain_result(f"#{rid} 删了")

    async def _add_reminder(self, event: AstrMessageEvent, gm: int, text: str) -> dict:
        real, game = await self._anchor()
        fire_at = remind_real_time(gm, real, game)
        items = load_reminders()
        rid = max([int(x.get("id", 0)) for x in items] + [0]) + 1
        item = {
            "id": rid,
            "target_game_min": int(gm),
            "target_game_clock": _fmt_game(gm),
            "text": text,
            "target_real": fire_at,
            "real_clock": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(fire_at)),
            "umo": event.unified_msg_origin,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "done": False,
            "tries": 0,
        }
        items.append(item)
        save_reminders(items)
        return item

    # ---------- 后台巡检 ----------

    async def _watch_loop(self) -> None:
        while True:
            try:
                await self._check_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.logger.error(f"rf4 提醒巡检出错：{exc}")
            await asyncio.sleep(TICK_SECONDS)

    async def _check_due(self) -> None:
        items = load_reminders()
        if not items:
            return
        now = time.time()
        keep: list[dict] = []
        changed = False
        for it in items:
            if it.get("done"):
                changed = True
                continue
            target = float(it.get("target_real", 0))
            if now < target:
                keep.append(it)
                continue
            if now - target > LATE_TOLERANCE:
                self.logger.warning(
                    f"rf4 提醒 #{it.get('id')} 错过太久，丢弃：{it.get('text')}"
                )
                changed = True
                continue
            ok = await self._send_reminder(it)
            if ok:
                changed = True
            else:
                it["tries"] = int(it.get("tries", 0)) + 1
                changed = True
                if it["tries"] < MAX_TRIES:
                    keep.append(it)
                else:
                    self.logger.error(
                        f"rf4 提醒 #{it.get('id')} 发了 {MAX_TRIES} 次都没成功，丢弃"
                    )
        if changed:
            save_reminders(keep)
        elif keep:
            save_reminders(keep)

    async def _send_reminder(self, it: dict) -> bool:
        umo = it.get("umo")
        if not umo:
            return False
        msg = (
            f"游戏里 {it.get('target_game_clock', _fmt_game(it.get('target_game_min', 0)))} 了\n"
            f"{it.get('text', '到点了')}"
        )
        try:
            await self.context.send_message(umo, MessageChain().message(msg))
            return True
        except Exception as exc:  # noqa: BLE001
            self.logger.error(f"rf4 提醒发送失败：{exc}")
            return False

    # ---------- LLM 工具 ----------

    @filter.llm_tool(name="rf4_game_time")
    async def rf4_game_time(self, event: AstrMessageEvent):
        """查询游戏《俄罗斯钓鱼4》里的当前游戏时间，以及离天亮或天黑还有多久。
        当用户问「游戏里几点了」「还有多久天黑/天亮」之类问题时调用。

        Args:
        """
        return await self._report(with_hint=False)

    @filter.llm_tool(name="rf4_set_reminder")
    async def rf4_set_reminder(self, event: AstrMessageEvent, game_time: str, text: str):
        """按游戏内时间设一个提醒，到点会主动发消息。用户说「游戏时间 18:00 提醒我打窝」这类话时调用。

        Args:
            game_time(string): 游戏内时间，格式 HH:MM，比如 18:00
            text(string): 到点要提醒的内容，比如 打窝
        """
        gm = _parse_hhmm(game_time)
        if gm is None:
            return "时间没看懂，给我 HH:MM 这种格式"
        item = await self._add_reminder(event, gm, text.strip() or "到点了")
        fire_at = item["target_real"]
        return (
            f"已设：游戏内 {_fmt_game(gm)} 提醒「{item['text']}」，"
            f"现实 {time.strftime('%H:%M:%S', time.localtime(fire_at))} 触发"
            f"（约 {_fmt_duration(fire_at - time.time())}后）"
        )

    @filter.llm_tool(name="rf4_set_offset")
    async def rf4_set_offset(self, event: AstrMessageEvent, minutes: int):
        """校准游戏时间的偏差补偿，正数往后推、负数往前拨、0 清零。
        用户说「游戏时间快了 3 分钟，往前拨一点」「时间补偿改成 5 分钟」这类话时调用。

        Args:
            minutes(number): 补偿的游戏分钟数，正负皆可，例如 -3 或 5
        """
        try:
            val = max(-1440, min(1440, int(minutes)))
        except (TypeError, ValueError):
            return "分钟数没看懂，给我个整数"
        self._offset = set_offset(val)
        persisted = self._persist_offset(val)
        real, game = await self._anchor()
        now_gm = game_minutes_now(real, game)
        moved = self._reschedule(now_gm)
        tail = f"，顺手重算了 {moved} 条提醒" if moved else ""
        if not persisted:
            tail += "（配置没写成，只在内存生效）"
        return f"补偿调成 {val:+d} 分钟，现在游戏内 {_fmt_game(now_gm)}{tail}"
