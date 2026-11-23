import pytest
import gameplay.skill_cooldown_tick as mod

CASES = [
    ("timestamp_tick", (1,)),
    ("pause_freeze", (1,)),
    ("per_instance_cd", (1,)),
    ("event_same_frame", (1,)),
    ("replay_clock", (1,)),
    ("determinism", (1,)),
    ("idempotent_query", (1,)),
    ("validation", (1,)),
    ("audit", (1,)),
    ("fallback", (1,)),
    ("ui_lead", (1,)),
    ("metrics", (1,)),
    ("cooldown_state", (1,)),
    ("haste", (1,)),
    ("charges", (1,)),
]

@pytest.mark.parametrize("fn_name,args", CASES)
def test_contract(fn_name, args):
    fn = getattr(mod, fn_name)
    assert fn(*args) is not None
