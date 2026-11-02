"""Behavioral regression checks; no game, controls or saved models touched."""
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import numpy as np
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/"assetto_corsa_gym")]
from AssettoCorsaEnv.progress_reward import progress_reward,CleanLapTracker
from AssettoCorsaEnv.gear_shift import verify_first_before_driving
from ac_workers.policy_evaluation import start_evaluation,finish_evaluation
from ac_workers.drive_truth import stationary_cause


def state(t=1.,f=.2,speed=0.):
    return dict(timestamp_ac=t,NormalizedSplinePosition=f,world_position_x=0.,world_position_y=0.,speed=speed)


def test_reverse_is_not_rewarded():
    a=state(speed=10);b=state(1.04,.1996,10);b['world_position_x']=-.4
    r=progress_reward(a,b,1000,NS(reward_motion_per_kmh=.003),np.zeros(3))
    assert r['progress_valid'] and r['reward']<0 and r['reward_motion']==0


def test_time_penalty_independent_of_sample_frequency():
    cfg=NS(reward_idle_penalty=.08,reward_step_cost=.002)
    def total(hz):
        return sum(progress_reward(state(i/hz),state((i+1)/hz),1000,cfg,np.zeros(3))['reward'] for i in range(hz))
    assert total(10)==pytest.approx(total(25))
    assert total(10)==pytest.approx(-2.05)


def test_invalid_sector_cannot_score():
    tracker=CleanLapTracker()
    tracker.update(state(f=.249),0,True,1000)
    assert tracker.update(state(f=.251),0,False,1000)['reward_clean_sector']==0
    assert tracker.forward_m==0


def test_partial_sector_after_reset_does_not_score():
    tracker=CleanLapTracker()
    tracker.update(state(f=.249),0,True,1000)
    assert tracker.update(state(f=.251),2,True,1000)['reward_clean_sector']==0
    assert tracker.update(state(f=.501),250,True,1000)['reward_clean_sector']==10


def test_first_requires_two_fresh_consecutive_packets():
    client=NS(state={'actualGear':1})
    packets=iter([{'actualGear':2,'timestamp_ac':1}, {'actualGear':2,'timestamp_ac':1},
                  {'actualGear':1,'timestamp_ac':2}, {'actualGear':2,'timestamp_ac':3},
                  {'actualGear':2,'timestamp_ac':4}])
    def step():
        client.state=next(packets);return client.state
    client.step_sim=step
    with patch('AssettoCorsaEnv.gear_shift.pulse_shift_up') as pulse:
        ok,report=verify_first_before_driving(client,{},clock=lambda:0)
    assert ok and report['samples']==[2,2,1,2,2]
    pulse.assert_called_once()


def test_stale_gear_packets_fail_boundedly():
    client=NS(state={'actualGear':2},step_sim=Mock(return_value={'actualGear':2,'timestamp_ac':1}))
    ok,report=verify_first_before_driving(client,{},clock=lambda:0)
    assert not ok and report['error']=='first_gear_not_confirmed'
    assert client.step_sim.call_count==300


@pytest.mark.parametrize('gear,gas,brake,expected',[(1,0,0,'neutral_gear'),(2,0,.5,'braking_at_stop'),
    (2,0,0,'throttle_unacknowledged'),(2,.8,0,'car_stuck')])
def test_stationary_reason(gear,gas,brake,expected):
    assert stationary_cause(dict(actualGear=gear,accStatus=gas,brakeStatus=brake),.8)==expected


@pytest.mark.parametrize(
    "cause,gear,gas,throttle,expect_reset",
    [
        ("throttle_unacknowledged", 2, 0.0, 0.8, True),
        ("neutral_gear", 1, 0.0, 0.8, True),   # deadlock: want gas, still in N
        ("neutral_gear", 1, 0.6, 0.8, True),   # gas seen but AUTO_SHIFTER stuck in N
        ("neutral_gear", 1, 0.0, 0.0, False),  # policy coasting — do not force reset
        ("car_stuck", 2, 0.8, 0.8, False),     # env stuck timer owns this
        ("braking_at_stop", 2, 0.0, 0.8, False),
    ],
)
def test_should_reset_for_stationary(cause, gear, gas, throttle, expect_reset):
    from ac_workers.drive_truth import should_reset_for_stationary

    assert (
        should_reset_for_stationary(
            cause, dict(actualGear=gear, accStatus=gas, brakeStatus=0.0), throttle
        )
        is expect_reset
    )

def test_evaluation_loads_before_freeze_and_resumes():
    policy=Mock();policy.has_pending_swap.return_value=False
    runtime=NS(policy=policy,_evaluation_restore_paused=False,_policy_paused=True)
    def create(*args):
        policy.detect_pending_version.assert_called_once()
        policy.apply_pending_policy.assert_called_once_with(9)
        return NS(training_step=123)
    with patch('ac_workers.policy_evaluation.EvaluationRun',side_effect=create):
        start_evaluation(runtime,None,{},1000,9)
    finish_evaluation(runtime)
    assert runtime.evaluation is None and not runtime._policy_paused
    runtime._evaluation_restore_paused=True
    finish_evaluation(runtime)
    assert runtime._policy_paused


def test_evaluation_refuses_stale_weights():
    policy=Mock();policy.has_pending_swap.return_value=True
    with patch('ac_workers.policy_evaluation.EvaluationRun') as ctor:
        with pytest.raises(RuntimeError,match='reload_failed'):
            start_evaluation(NS(policy=policy),None,{},1000,9)
        ctor.assert_not_called()


def test_reset_rejects_repeated_telemetry_and_accepts_fresh_packets():
    from ac_workers.collector_live_attach import wait_for_consistent_reset_obs
    class Env:
        ep_steps=10
        start_actions=np.zeros(3)
        def __init__(self, fresh):
            self.state=state();self.fresh=fresh;self.i=0
        def step(self, action):
            self.i+=1
            self.state=state(1+self.i*.04 if self.fresh else 1)
            return np.zeros(3),0,False,{}
    assert not wait_for_consistent_reset_obs(Env(False))[0]
    assert wait_for_consistent_reset_obs(Env(True))[0]


def test_actual_termination_clock_accumulates_seconds_not_steps():
    # Execute the production clock block with different observed frequencies.
    source=(ROOT/'assetto_corsa_gym/AssettoCorsaEnv/ac_env.py').read_text()
    start=source.index('        # Count actual telemetry time')
    end=source.index('        if self._force_episode_done',start)
    import textwrap,time
    code=compile(textwrap.dedent(source[start:end]),'termination_clock','exec')
    for hz in [10,25]:
        env=NS(ctrl_rate=25,oot_grace_sec=3.)
        for i in range(hz*4+1):
            scope={'self':env,'state':state(i/hz),'time':time}
            exec(code,scope)
        assert env._driving_elapsed==pytest.approx(4)
        assert not scope['in_post_reset_grace']
        assert scope['elapsed_steps']==pytest.approx(25/hz)


def test_first_gear_ignores_loading_backlog_before_shifting():
    client=NS(state={'actualGear':1})
    client.discard_pending_telemetry=Mock(return_value=120)
    def step():
        client.state={'actualGear':2,'timestamp_ac':step.i}
        step.i+=1
        return client.state
    step.i=1
    client.step_sim=step
    with patch('AssettoCorsaEnv.gear_shift.pulse_shift_up') as pulse:
        ok,report=verify_first_before_driving(client,{},clock=lambda:0)
    assert ok
    pulse.assert_not_called()
    assert report['discarded_packets']==240
