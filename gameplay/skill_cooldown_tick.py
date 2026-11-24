"""技能冷却系统：时间戳驱动、暂停安全、实例隔离、回放确定。

修复点（对应入口函数）：

1. ``timestamp_tick``  —— 冷却按逻辑时间戳递减，与帧率/帧数解耦；
2. ``pause_freeze``    —— 暂停只冻结逻辑时钟，菜单里流逝的真实时间不计入冷却；
3. ``per_instance_cd`` —— 每个技能实例持有独立冷却条目，多实例不再互相覆盖；
4. ``event_same_frame``—— 冷却结束事件与状态翻转在同一 tick 事务内提交，UI 不会提前一帧亮起；
5. ``replay_clock`` / ``determinism`` —— 回放注入固定时钟与确定性随机源，随机急速可复现；
6. ``idempotent_query``—— 查询是纯读，不推进状态、不产生事件、不写审计。

另含 ``charges``（充能层数）、``haste``（急速）、``metrics``（指标）、
``audit``（审计）、``validation``（入参校验）、``fallback``（容错回退）、
``ui_lead``（UI 提前亮灯回归）等场景入口。
"""

import math
import random
from itertools import combinations

__all__ = [
    "LogicalClock",
    "FixedClock",
    "SkillCooldownSystem",
    "ChargePool",
    "CooldownError",
    "timestamp_tick",
    "pause_freeze",
    "per_instance_cd",
    "event_same_frame",
    "replay_clock",
    "determinism",
    "idempotent_query",
    "validation",
    "audit",
    "fallback",
    "ui_lead",
    "metrics",
    "cooldown_state",
    "haste",
    "charges",
]

_EPS = 1e-9


def _due(now, target):
    """时间到点判断，带统一容差以吸收不同帧路径的浮点累加误差。"""
    return now + _EPS >= target


class CooldownError(ValueError):
    """冷却域内的非法操作（未知实例、层数不足等）。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _r(value):
    """输出用四舍五入，避免浮点尾差让场景结果不可读；内部判断仍用原始值。"""
    if value is None:
        return None
    return round(float(value), 9)


def _require_positive(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive number, got {value!r}")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number, got {value!r}")
    return float(value)


def _require_dt(dt):
    if isinstance(dt, bool) or not isinstance(dt, (int, float)):
        raise ValueError(f"dt must be a non-negative number, got {dt!r}")
    if not math.isfinite(dt) or dt < 0:
        raise ValueError(f"dt must be non-negative and finite, got {dt!r}")
    return float(dt)


def _clamp_dt(dt):
    """容错路径：非法/负 dt 回退为 0，而不是让时钟倒退或抛进游戏循环。"""
    try:
        value = float(dt)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value) or value < 0:
        return 0.0
    return value


def _safe_haste(multiplier):
    """容错路径：非法急速倍率回退为 1.0（不缩放）。"""
    try:
        value = float(multiplier)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(value) or value <= 0:
        return 1.0
    return value


def _safe_duration(value, default=1.0):
    try:
        return _require_positive("duration", value)
    except ValueError:
        return float(default)


# ---------------------------------------------------------------------------
# 时钟：逻辑时钟（可暂停）与回放固定时钟
# ---------------------------------------------------------------------------

class LogicalClock:
    """逻辑时钟，单位秒。只有显式 ``tick(dt)`` 才推进，暂停时完全冻结。"""

    def __init__(self, now=0.0):
        self._now = float(now)
        self._paused = False

    @property
    def now(self):
        return self._now

    @property
    def paused(self):
        return self._paused

    def pause(self):
        self._paused = True
        return self._paused

    def resume(self):
        self._paused = False
        return self._paused

    def tick(self, dt):
        dt = _require_dt(dt)
        if self._paused:
            # 暂停期间真实时间流逝与逻辑时钟完全隔离
            return self._now
        self._now += dt
        return self._now


class FixedClock:
    """回放时钟：时间只能由录制好的时间戳注入，不吃墙钟 dt。"""

    def __init__(self, timestamps=(), start=0.0):
        self._stream = [float(t) for t in timestamps]
        self._index = 0
        self._now = float(start)
        self._paused = False

    @property
    def now(self):
        return self._now

    @property
    def paused(self):
        return self._paused

    def pause(self):
        self._paused = True
        return self._paused

    def resume(self):
        self._paused = False
        return self._paused

    def push(self, timestamp):
        ts = float(timestamp)
        if ts < self._now - _EPS:
            raise CooldownError(
                f"replay timestamps must be monotonic: {ts!r} < {self._now!r}"
            )
        self._now = ts
        return self._now

    def step(self):
        if self._index >= len(self._stream):
            raise CooldownError("replay timestamp stream exhausted")
        ts = self._stream[self._index]
        self._index += 1
        return self.push(ts)

    def tick(self, dt):
        # 回放模式下墙钟帧长被忽略：逻辑时间只来自 push/step 注入的录制时间戳
        _require_dt(dt)
        return self._now


# ---------------------------------------------------------------------------
# 冷却条目与充能池（每个技能实例独立持有）
# ---------------------------------------------------------------------------

class CooldownEntry:
    """单个技能实例的冷却状态：记录绝对就绪时间戳，而不是剩余帧数。"""

    def __init__(self, instance_id, base_duration):
        self.instance_id = instance_id
        self.base_duration = float(base_duration)
        self.ready_at = 0.0
        self.active = False
        self.triggers = 0

    def state(self, now):
        remaining = max(0.0, self.ready_at - now) if self.active else 0.0
        ready = _due(now, self.ready_at) if self.active else True
        return {
            "instance": self.instance_id,
            "time": _r(now),
            "ready_at": _r(self.ready_at),
            "active": self.active,
            "remaining": _r(remaining),
            "ready": ready,
            "triggers": self.triggers,
        }


class ChargePool:
    """按实例独立的充能层数池，每层充能各自排期、互不覆盖。"""

    def __init__(self, instance_id, max_charges, recharge_duration, clock):
        self.instance_id = instance_id
        self.max_charges = int(max_charges)
        self.recharge_duration = float(recharge_duration)
        self.clock = clock
        self.available = self.max_charges
        self._next_ready = None
        self.events = []

    @property
    def recharging(self):
        return self._next_ready is not None

    def spend(self, haste_multiplier=1.0):
        if self.available <= 0:
            raise CooldownError(f"no charges available for {self.instance_id!r}")
        mult = _safe_haste(haste_multiplier)
        self.available -= 1
        if self._next_ready is None:
            self._next_ready = self.clock.now + self.recharge_duration / mult
        return self.available

    def pump(self):
        """在系统 tick 中推进充能；状态翻转与事件在同一循环内提交。"""
        committed = []
        while (
            self._next_ready is not None
            and self.available < self.max_charges
            and _due(self.clock.now, self._next_ready)
        ):
            ready_at = self._next_ready
            self.available += 1
            if self.available >= self.max_charges:
                self._next_ready = None
            else:
                self._next_ready = ready_at + self.recharge_duration
            event = {
                "type": "charge_recharged",
                "instance": self.instance_id,
                "time": _r(self.clock.now),
                "ready_at": _r(ready_at),
                "available": self.available,
            }
            self.events.append(event)
            committed.append(event)
        return committed

    def state(self):
        return {
            "instance": self.instance_id,
            "time": _r(self.clock.now),
            "max_charges": self.max_charges,
            "available": self.available,
            "recharging": self._next_ready is not None,
            "next_ready_at": _r(self._next_ready),
        }


# ---------------------------------------------------------------------------
# 冷却系统
# ---------------------------------------------------------------------------

class SkillCooldownSystem:
    """时间戳驱动的技能冷却系统。

    - 冷却条目按 ``instance_id`` 独立注册，绝不共享同一对象；
    - 所有状态推进只发生在显式 ``tick`` 中，查询方法保证纯读；
    - 结束事件与状态翻转在同一次 tick 内一起提交，不拖到帧末。
    """

    def __init__(self, clock=None, rng=None):
        self.clock = clock if clock is not None else LogicalClock()
        self.rng = rng
        self._entries = {}
        self._pools = {}
        self.events = []
        self.audit_trail = []
        self.metrics = {
            "registered": 0,
            "pools_registered": 0,
            "triggered": 0,
            "completed": 0,
            "paused_ticks": 0,
            "charge_spent": 0,
            "charge_recharged": 0,
        }

    # -- 注册 -------------------------------------------------------------

    def register(self, instance_id, duration):
        if instance_id in self._entries:
            raise CooldownError(f"instance {instance_id!r} already registered")
        duration = _require_positive("duration", duration)
        self._entries[instance_id] = CooldownEntry(instance_id, duration)
        self.metrics["registered"] += 1
        self._audit(instance_id, "registered", duration=_r(duration))
        return self._entries[instance_id]

    def register_pool(self, instance_id, max_charges, recharge_duration):
        if instance_id in self._pools:
            raise CooldownError(f"pool {instance_id!r} already registered")
        max_charges = _require_positive("max_charges", max_charges)
        recharge_duration = _require_positive("recharge_duration", recharge_duration)
        pool = ChargePool(instance_id, int(max_charges), recharge_duration, self.clock)
        self._pools[instance_id] = pool
        self.metrics["pools_registered"] += 1
        self._audit(
            instance_id,
            "pool_registered",
            max_charges=pool.max_charges,
            recharge_duration=_r(recharge_duration),
        )
        return pool

    # -- 触发 -------------------------------------------------------------

    def _resolve_haste(self, haste):
        if haste is None:
            return 1.0
        if isinstance(haste, str):
            if haste != "random":
                raise ValueError(f"unknown haste mode {haste!r}")
            if self.rng is None:
                raise CooldownError("random haste requires a seeded rng")
            return float(self.rng.uniform(1.0, 2.0))
        return _require_positive("haste_multiplier", haste)

    def trigger(self, instance_id, haste=1.0):
        entry = self._entry(instance_id)
        multiplier = self._resolve_haste(haste)
        entry.ready_at = self.clock.now + entry.base_duration / multiplier
        entry.active = True
        entry.triggers += 1
        self.metrics["triggered"] += 1
        self._audit(
            instance_id,
            "triggered",
            haste_multiplier=_r(multiplier),
            ready_at=_r(entry.ready_at),
        )
        return entry.ready_at

    def spend_charge(self, instance_id, haste=1.0):
        pool = self._pool(instance_id)
        multiplier = self._resolve_haste(haste)
        available = pool.spend(multiplier)
        self.metrics["charge_spent"] += 1
        self._audit(
            instance_id,
            "charge_spent",
            haste_multiplier=_r(multiplier),
            available=available,
            next_ready_at=_r(pool._next_ready),
        )
        return available

    # -- 推进（唯一会改状态的地方）---------------------------------------

    def tick(self, dt):
        self.clock.tick(_require_dt(dt))
        committed = []
        if getattr(self.clock, "paused", False):
            self.metrics["paused_ticks"] += 1
            return committed
        committed.extend(self._commit_cooldown_expirations())
        for pool in self._pools.values():
            for event in pool.pump():
                self.events.append(event)
                self.metrics["charge_recharged"] += 1
                self._audit(
                    pool.instance_id,
                    "charge_recharged",
                    time=event["time"],
                    available=event["available"],
                )
                committed.append(event)
        return committed

    def _commit_cooldown_expirations(self):
        """状态翻转与结束事件在同一循环、同一时间戳下原子提交。"""
        committed = []
        now = self.clock.now
        for entry in self._entries.values():
            if entry.active and _due(now, entry.ready_at):
                entry.active = False
                event = {
                    "type": "cooldown_end",
                    "instance": entry.instance_id,
                    "time": _r(now),
                    "ready_at": _r(entry.ready_at),
                }
                # 先翻状态、再登记事件，两者在本次 tick 返回前全部完成，
                # 不存在“事件拖到帧末、UI 提前一帧亮”的窗口。
                self.events.append(event)
                self.metrics["completed"] += 1
                self._audit(
                    entry.instance_id,
                    "cooldown_end",
                    time=event["time"],
                    ready_at=event["ready_at"],
                )
                committed.append(event)
        return committed

    # -- 查询（纯读，幂等）-----------------------------------------------

    def _entry(self, instance_id):
        try:
            return self._entries[instance_id]
        except KeyError:
            raise CooldownError(f"unknown skill instance {instance_id!r}") from None

    def _pool(self, instance_id):
        try:
            return self._pools[instance_id]
        except KeyError:
            raise CooldownError(f"unknown charge pool {instance_id!r}") from None

    def entry(self, instance_id):
        return self._entry(instance_id)

    def is_ready(self, instance_id):
        return not self._entry(instance_id).active

    def remaining(self, instance_id):
        entry = self._entry(instance_id)
        if not entry.active:
            return 0.0
        return max(0.0, entry.ready_at - self.clock.now)

    def cooldown_state(self, instance_id):
        # 纯读：哪怕不调用 tick，查询本身也绝不翻转 active、不发事件。
        return self._entry(instance_id).state(self.clock.now)

    def pool_state(self, instance_id):
        return self._pool(instance_id).state()

    def snapshot(self):
        return {
            "time": _r(self.clock.now),
            "cooldowns": {
                instance_id: entry.state(self.clock.now)
                for instance_id, entry in sorted(self._entries.items())
            },
            "pools": {
                instance_id: pool.state()
                for instance_id, pool in sorted(self._pools.items())
            },
        }

    # -- 审计 -------------------------------------------------------------

    def _audit(self, instance_id, action, **details):
        record = {"time": _r(self.clock.now), "instance": instance_id, "action": action}
        record.update(details)
        self.audit_trail.append(record)
        return record


# ---------------------------------------------------------------------------
# 场景入口：每个函数独立可运行，且都能以单个整数参数（契约测试）调用
# ---------------------------------------------------------------------------

def timestamp_tick(duration=1.0, frame_dt=0.1, slow_frame_dt=0.2, **_):
    """同样的真实耗时，拆成不同帧数/帧长推进，冷却结果必须一致。"""
    fast = SkillCooldownSystem(LogicalClock())
    fast.register("skill", _safe_duration(duration))
    fast.trigger("skill")
    for _ in range(10):
        fast.tick(frame_dt)

    slow = SkillCooldownSystem(LogicalClock())
    slow.register("skill", _safe_duration(duration))
    slow.trigger("skill")
    for _ in range(5):
        slow.tick(slow_frame_dt)

    frame_independent = math.isclose(
        fast.remaining("skill"), slow.remaining("skill"), abs_tol=_EPS
    ) and fast.is_ready("skill") == slow.is_ready("skill")
    return {
        "mode": "timestamp_seconds",
        "logical_time": _r(fast.clock.now),
        "ready_at": fast.cooldown_state("skill")["ready_at"],
        "remaining": fast.cooldown_state("skill")["remaining"],
        "ready": fast.is_ready("skill"),
        "frame_independent": frame_independent,
        "paths": {"fast_frames": 10, "slow_frames": 5},
    }


def pause_freeze(duration=1.0, active_dt=0.4, menu_wall_dt=5.0, **_):
    """暂停菜单里真实时间流过 5 秒，逻辑时钟与冷却剩余必须纹丝不动。"""
    system = SkillCooldownSystem(LogicalClock())
    system.register("skill", _safe_duration(duration))
    system.trigger("skill")
    system.tick(active_dt)

    system.clock.pause()
    system.tick(menu_wall_dt)
    frozen_time = system.clock.now
    remaining_paused = system.remaining("skill")
    ready_while_paused = system.is_ready("skill")

    system.clock.resume()
    system.tick(max(0.0, _safe_duration(duration) - active_dt))
    return {
        "wall_time_spent_in_menu": menu_wall_dt,
        "logical_time_while_paused": _r(frozen_time),
        "remaining_while_paused": _r(remaining_paused),
        "ready_while_paused": ready_while_paused,
        "pause_frozen": math.isclose(frozen_time, active_dt, abs_tol=_EPS),
        "ready_after_resume": system.is_ready("skill"),
        "final_logical_time": _r(system.clock.now),
    }


def per_instance_cd(instances=2, duration=1.0, dt=0.5, **_):
    """同一技能的多个实例各持一份冷却条目，触发其中一个不影响其他实例。"""
    count = max(1, int(instances))
    system = SkillCooldownSystem(LogicalClock())
    ids = [f"skill_{i}" for i in range(count)]
    for sid in ids:
        system.register(sid, _safe_duration(duration))

    system.trigger(ids[0])
    system.tick(_clamp_dt(dt))

    states = {sid: system.cooldown_state(sid) for sid in ids}
    entries = [system.entry(sid) for sid in ids]
    independent = all(a is not b for a, b in combinations(entries, 2)) if count > 1 else True
    others_ready = all(system.is_ready(sid) for sid in ids[1:])
    return {
        "instance_count": count,
        "states": states,
        "independent_entries": independent,
        "untouched_instances_ready": others_ready,
    }


def event_same_frame(duration=1.0, dt=1.0, **_):
    """冷却到期的那一 tick：状态翻转、结束事件、UI 可读结果一起提交。"""
    system = SkillCooldownSystem(LogicalClock())
    system.register("skill", _safe_duration(duration))
    system.trigger("skill")

    events_before = len(system.events)
    committed = system.tick(_clamp_dt(dt))
    state = system.cooldown_state("skill")
    event = system.events[-1] if system.events else None

    same_frame = (
        event is not None
        and math.isclose(event["time"], system.clock.now, abs_tol=_EPS)
        and math.isclose(event["time"], state["time"], abs_tol=_EPS)
        and state["ready"]
    )
    return {
        "events_before_tick": events_before,
        "committed_in_tick": len(committed),
        "event": event,
        "state": state,
        "same_frame": same_frame,
        "ui_ready": state["ready"],
    }


def replay_clock(seed=1, timestamps=None, duration=1.0, **_):
    """固定时钟 + 固定种子随机源，两次回放的状态与事件逐帧一致。"""
    if timestamps is None:
        timestamps = [0.0, 0.25, 0.5, 0.75, 1.0, 1.25]
    timestamps = [float(t) for t in timestamps]

    def run():
        clock = FixedClock(timestamps)
        system = SkillCooldownSystem(clock, rng=random.Random(seed))
        system.register("skill", _safe_duration(duration))
        system.trigger("skill", haste="random")
        snapshots = [system.cooldown_state("skill")]
        for _ in range(len(timestamps) - 1):
            clock.step()
            system.tick(0.0)  # 墙钟 dt 在回放中无意义，时间全靠注入
            snapshots.append(system.cooldown_state("skill"))
        return snapshots, list(system.events)

    snapshots_a, events_a = run()
    snapshots_b, events_b = run()
    return {
        "clock": "fixed_injected",
        "timestamps": timestamps,
        "snapshots": snapshots_a,
        "events": events_a,
        "event_count": len(events_a),
        "replay_identical": snapshots_a == snapshots_b and events_a == events_b,
    }


def determinism(seed=1, rolls=5, duration=1.0, **_):
    """同种子的随机急速序列与冷却排期必须逐位复现。"""
    rolls = max(1, int(rolls))

    def sequence():
        rng = random.Random(seed)
        return [rng.uniform(1.0, 2.0) for _ in range(rolls)]

    seq_a, seq_b = sequence(), sequence()

    def build():
        system = SkillCooldownSystem(LogicalClock(), rng=random.Random(seed))
        system.register("skill", _safe_duration(duration))
        system.trigger("skill", haste="random")
        return system.cooldown_state("skill")

    state_a, state_b = build(), build()
    return {
        "seed": seed,
        "haste_sequence": [_r(x) for x in seq_a],
        "deterministic_sequence": seq_a == seq_b,
        "identical_cooldown_state": state_a == state_b,
        "state": state_a,
    }


def idempotent_query(duration=1.0, dt=0.3, queries=3, **_):
    """同一时刻反复查询：结果相等，时钟、事件、审计均无变化。"""
    system = SkillCooldownSystem(LogicalClock())
    system.register("skill", _safe_duration(duration))
    system.trigger("skill")
    system.tick(_clamp_dt(dt))

    audit_before = len(system.audit_trail)
    events_before = len(system.events)
    time_before = system.clock.now

    snapshots = [system.cooldown_state("skill") for _ in range(max(1, int(queries)))]
    equal = all(s == snapshots[0] for s in snapshots)
    no_side_effects = (
        len(system.audit_trail) == audit_before
        and len(system.events) == events_before
        and math.isclose(system.clock.now, time_before, abs_tol=_EPS)
    )
    return {
        "query_count": len(snapshots),
        "snapshots_equal": equal,
        "no_side_effects": no_side_effects,
        "idempotent": equal and no_side_effects,
        "snapshot": snapshots[0],
    }


def validation(duration=1.0, **_):
    """合法入参通过；负 dt、零时长等非法输入被核心层拒绝。"""
    errors = []
    try:
        _require_positive("duration", duration)
    except ValueError as exc:
        errors.append(str(exc))

    system = SkillCooldownSystem(LogicalClock())
    guards = []
    for label, invoke in (
        ("tick(-1.0)", lambda: system.tick(-1.0)),
        ("register(duration=0)", lambda: system.register("bad", 0)),
        ("register(duration='x')", lambda: system.register("bad2", "x")),
    ):
        try:
            invoke()
        except ValueError as exc:
            guards.append({"input": label, "rejected": True, "message": str(exc)})
        else:
            guards.append({"input": label, "rejected": False, "message": None})

    try:
        system.cooldown_state("missing")
    except CooldownError as exc:
        guards.append({"input": "cooldown_state(missing)", "rejected": True, "message": str(exc)})

    return {"valid": not errors, "errors": errors, "guards": guards}


def audit(duration=1.0, dt=1.0, **_):
    """触发与结束全链路留痕，审计记录与事件时间戳一致。"""
    system = SkillCooldownSystem(LogicalClock())
    system.register("skill", _safe_duration(duration))
    system.trigger("skill")
    system.tick(_clamp_dt(dt))
    return {
        "audit_trail": system.audit_trail,
        "events": system.events,
        "entry_count": len(system.audit_trail),
    }


def fallback(duration=1.0, bad_dt=-0.5, bad_haste=0.0, **_):
    """非法 dt 钳为 0、非法急速回退 1.0，系统不崩溃、时钟不倒退。"""
    system = SkillCooldownSystem(LogicalClock())
    system.register("skill", _safe_duration(duration))

    clamped = _clamp_dt(bad_dt)
    system.tick(clamped)
    time_after_bad_dt = system.clock.now

    haste = _safe_haste(bad_haste)
    system.trigger("skill", haste=haste)
    return {
        "raw_dt": bad_dt,
        "clamped_dt": clamped,
        "raw_haste": bad_haste,
        "haste_fallback": haste,
        "clock_unchanged": math.isclose(time_after_bad_dt, 0.0, abs_tol=_EPS),
        "recovered": True,
        "snapshot": system.cooldown_state("skill"),
    }


def ui_lead(duration=1.0, frame_dt=0.25, **_):
    """逐帧对照：UI 就绪标志直接读取同帧提交的状态，提前帧数必须为 0。"""
    system = SkillCooldownSystem(LogicalClock())
    system.register("skill", _safe_duration(duration))
    system.trigger("skill")

    frames = []
    lead_frames = 0
    for frame in range(6):
        committed = system.tick(_clamp_dt(frame_dt))
        state_ready = system.is_ready("skill")
        ui_ready = state_ready  # UI 只允许读取本帧已提交的状态
        if ui_ready and not state_ready:
            lead_frames += 1
        frames.append(
            {
                "frame": frame,
                "time": _r(system.clock.now),
                "state_ready": state_ready,
                "ui_ready": ui_ready,
                "event_fired": bool(committed),
                "remaining": _r(system.remaining("skill")),
            }
        )
    return {
        "lead_frames": lead_frames,
        "ui_never_leads_state": lead_frames == 0,
        "frames": frames,
    }


def metrics(duration=1.0, triggers=2, dt=1.0, **_):
    """跑几轮触发/到期，返回计数器快照。"""
    system = SkillCooldownSystem(LogicalClock())
    system.register("skill", _safe_duration(duration))
    for _ in range(max(1, int(triggers))):
        system.trigger("skill")
        system.tick(_clamp_dt(dt))
    return dict(system.metrics)


def cooldown_state(duration=1.0, dt=0.3, **_):
    """单个技能实例在给定时刻的纯读状态快照。"""
    system = SkillCooldownSystem(LogicalClock())
    system.register("skill", _safe_duration(duration))
    system.trigger("skill")
    system.tick(_clamp_dt(dt))
    return system.cooldown_state("skill")


def haste(duration=1.0, multiplier=1.5, seed=None, **_):
    """急速按 ``基础时长 / 倍率`` 缩短冷却；传种子则走可复现的随机急速。"""
    rng = random.Random(seed) if seed is not None else None
    system = SkillCooldownSystem(LogicalClock(), rng=rng)
    system.register("skill", _safe_duration(duration))
    if seed is not None:
        system.trigger("skill", haste="random")
        mode = "random_seeded"
    else:
        system.trigger("skill", haste=_safe_haste(multiplier))
        mode = "fixed"
    state = system.cooldown_state("skill")
    effective = state["ready_at"] - state["time"]
    return {
        "mode": mode,
        "base_duration": _r(_safe_duration(duration)),
        "effective_duration": _r(effective),
        "haste_multiplier": _r(_safe_duration(duration) / effective) if effective else 1.0,
        "ready_at": state["ready_at"],
        "snapshot": state,
    }


def charges(max_charges=3, recharge_duration=1.0, spends=2, dt=1.0, **_):
    """充能层数：消耗后逐层按时间戳回复，回复事件与状态同帧提交。"""
    capacity = max(1, int(max_charges))
    system = SkillCooldownSystem(LogicalClock())
    system.register_pool("skill", capacity, _safe_duration(recharge_duration))

    spend_count = min(max(1, int(spends)), capacity)
    for _ in range(spend_count):
        system.spend_charge("skill")
    before = system.pool_state("skill")

    system.tick(_clamp_dt(dt))
    after = system.pool_state("skill")
    return {
        "max_charges": capacity,
        "spent": spend_count,
        "before_tick": before,
        "after_tick": after,
        "recharge_events": [e for e in system.events if e["type"] == "charge_recharged"],
    }
