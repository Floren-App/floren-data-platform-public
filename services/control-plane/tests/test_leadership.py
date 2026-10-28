"""Several control plane replicas: one leader runs the background work, and leadership moves when it fails."""
import asyncio

from app import leadership
from app.db import engine
from app.leadership import Leadership, cluster_lock, lock_key


def test_lock_keys_are_stable_signed_64_bit_numbers():
    assert lock_key("fdp:control-plane:leader") == lock_key("fdp:control-plane:leader")
    assert lock_key("a") != lock_key("b") and -(2 ** 63) <= lock_key("a") < 2 ** 63


def test_only_the_leader_runs_background_work_and_stops_when_it_loses_leadership(monkeypatch):
    monkeypatch.setattr(leadership, "CHECK_SECONDS", 0.01)
    started, cancelled = [], []

    async def loop():
        started.append(1)
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(1)
            raise

    state = {"lock": False, "alive": True}
    replica = Leadership(engine, [loop, loop])
    monkeypatch.setattr(replica, "_try_acquire", lambda: state["lock"])
    monkeypatch.setattr(replica, "_still_leader", lambda: state["alive"])

    async def scenario():
        task = asyncio.create_task(replica.run())
        await asyncio.sleep(0.05)
        assert not replica.leader and started == []          # another replica holds the lock
        state["lock"] = True
        await asyncio.sleep(0.05)
        assert replica.leader and len(started) == 2          # it took over: its loops run
        state.update(alive=False, lock=False)                 # its session is gone; another replica took the lock
        await asyncio.sleep(0.05)
        assert not replica.leader and len(cancelled) == 2     # it stops at once
        state.update(alive=True, lock=True)                   # that replica failed in turn: this one leads again
        await asyncio.sleep(0.05)
        assert replica.leader and len(started) == 4
        task.cancel()
    asyncio.run(scenario())


def test_without_postgres_the_single_replica_leads_and_locks_are_local():
    assert Leadership(engine, [])._try_acquire() is True
    with cluster_lock(engine, "deploy:w1") as first:
        with cluster_lock(engine, "deploy:w1") as second:
            assert first and not second                        # one deploy per workload at a time
        with cluster_lock(engine, "deploy:w2") as other:
            assert other
    with cluster_lock(engine, "deploy:w1") as again:
        assert again
