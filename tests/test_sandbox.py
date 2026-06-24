from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_sandbox.agent import (
    AgentRunner,
    build_system_prompt,
    build_user_prompt,
    build_dialogue_control_frame,
    build_turn_frame,
    classify_input_mode,
    clean_response,
    count_recent_sora_question_streak,
    enforce_turn_frame,
    enforce_question_budget,
)
from sora_sandbox.commands import handle_command
from sora_sandbox.credentials import CredentialManager
from sora_sandbox.llm import FakeLLMClient
from sora_sandbox.prompt_loader import load_prompt_registry, render_system_prompt
from sora_sandbox.proactive import ProactiveScheduler
from sora_sandbox.render import render_trace_panel
from sora_sandbox.repl import parse_duration_seconds
from sora_sandbox.state import StateStore, append_recent_turn, seed_state
from sora_sandbox.subsystems import FunctionSubsystemAdapter, SubsystemRegistry


class SandboxTests(unittest.TestCase):
    def test_seed_state_contains_persona_worldops_and_memory(self) -> None:
        state = seed_state()
        self.assertEqual(state["persona"]["persona_core"]["name"], "Sora")
        self.assertIn("evt_starport_lamp_001", [event["event_id"] for event in state["worldops"]["content_pack"]["events"]])
        self.assertGreaterEqual(len(state["memory"]["items"]), 3)
        self.assertIn("origin", state)
        self.assertIn("template_id", state["origin"])
        self.assertIn("origin_bias", state["initiative"])
        self.assertEqual(state["behavior"]["current"]["preset"], "casual_idle")
        self.assertEqual(state["behavior"]["current"]["interruptibility"], "available")

    def test_set_trait_records_debug_override(self) -> None:
        state = seed_state()
        output, should_exit = handle_command("/set-trait playfulness 0.55 --debug", state)
        self.assertFalse(should_exit)
        self.assertIn("debug override", output)
        self.assertEqual(state["persona"]["persona_traits"]["playfulness"]["value"], 0.55)
        self.assertEqual(state["persona"]["debug_overrides"][-1]["target"], "persona_traits.playfulness")

    def test_worldops_micro_quest_creates_memory_hook(self) -> None:
        state = seed_state()
        handle_command("/start-micro-quest evt_starport_lamp_001", state)
        output, _ = handle_command('/complete-micro-quest evt_starport_lamp_001 --choice "写一盏送回星港的灯"', state)
        self.assertIn("memory_hook", output)
        self.assertEqual(state["worldops"]["micro_quest_sessions"][0]["status"], "completed")
        self.assertEqual(len(state["worldops"]["memory_hooks"]), 1)

    def test_agent_run_with_fake_llm_returns_contracts(self) -> None:
        state = seed_state()
        handle_command("/activate-world-event evt_starport_lamp_001", state)
        runner = AgentRunner(FakeLLMClient())
        result = runner.run(state, "你今天有什么自己的事想说吗？")
        self.assertIn("reply", result)
        self.assertIn("response_plan", result)
        self.assertIn("state_diff", result)
        self.assertEqual(result["state_diff"]["worldops_event_used"], "evt_starport_lamp_001")
        self.assertTrue(result["trace"])
        self.assertIn("event_bus", result["timing"])
        self.assertIn("context_frames", result["timing"])
        self.assertIn("subsystem_signals", result["timing"])
        self.assertIn("initiative_window", result["timing"])
        self.assertIn("arbitration", result["timing"])
        self.assertIn("response_planner", result["timing"])
        self.assertIn("response_planner", result["token_usage"])

    def test_prompt_registry_and_templates_render(self) -> None:
        registry = load_prompt_registry()
        self.assertEqual(registry.version, "sora_companion_v0.6")
        self.assertTrue(registry.system.exists())
        self.assertTrue(registry.user_context.exists())
        self.assertTrue(registry.dialogue_control.exists())

        system = render_system_prompt({"name": "Sora", "species": "灵构体"})
        self.assertIn("你是 Sora", system)
        self.assertIn("陪伴型数字生命回应编排器", system)
        self.assertIn("有连续自我感的数字生命", system)
        self.assertIn("不要总用", system)

    def test_agent_prompt_builders_use_external_templates(self) -> None:
        state = seed_state()
        runner = AgentRunner(FakeLLMClient())
        context_pack = runner.run(state, "你好呀")["context_pack"]
        system = build_system_prompt(context_pack)
        user = build_user_prompt(context_pack, "你好呀")
        self.assertIn("Project Sora", system)
        self.assertIn("context_pack", user)
        self.assertIn("turn_frame", user)
        self.assertIn("context_use_policy", user)
        self.assertIn("arbitration_result", user)
        self.assertIn("signals", user)
        self.assertIn("origin_frame", user)
        self.assertIn("dialogue_control_frame", user)
        self.assertIn("input_mode=greeting", user)
        self.assertIn("你好呀", user)

    def test_trace_panel_matches_poc_observability_shape(self) -> None:
        state = seed_state()
        handle_command("/activate-world-event evt_starport_lamp_001", state)
        result = AgentRunner(FakeLLMClient()).run(state, "你好呀")
        panel = render_trace_panel(result)
        self.assertIn("Sora Agent 轨迹", panel)
        self.assertIn("用户: 你好呀", panel)
        self.assertIn("微事件", panel)
        self.assertIn("Token:", panel)
        self.assertIn("短期上下文: 注入", panel)
        self.assertIn("Origin:", panel)
        self.assertIn("子系统协同", panel)
        self.assertIn("仲裁", panel)
        self.assertIn("Runtime phases", panel)
        self.assertIn("Signal timings", panel)
        self.assertIn("context_frames", panel)
        self.assertIn("response_planner", panel)
        self.assertIn("sig_dialogue", panel)

    def test_behavior_changed_is_consumed_after_one_run(self) -> None:
        state = seed_state()
        runner = AgentRunner(FakeLLMClient())
        first = runner.run(state, "第一句")
        second = runner.run(state, "第二句")
        self.assertTrue(first["context_pack_summary"]["behavior_changed"])
        self.assertFalse(second["context_pack_summary"]["behavior_changed"])

    def test_recent_turns_keep_twenty_rounds(self) -> None:
        state = seed_state()
        for idx in range(25):
            append_recent_turn(state, "user", f"u{idx}")
            append_recent_turn(state, "sora", f"s{idx}")
        turns = state["session"]["recent_turns"]
        self.assertEqual(len(turns), 40)
        self.assertEqual(turns[0]["content"], "u5")

    def test_dialogue_control_limits_repeated_questions(self) -> None:
        state = seed_state()
        append_recent_turn(state, "user", "最近有什么新鲜事儿吗")
        append_recent_turn(state, "sora", "我这边有个旧邮筒。你呢？")
        append_recent_turn(state, "user", "和我聊一下那个旧邮筒？")
        append_recent_turn(state, "sora", "它学人说话还挺像的。你怎么突然想起问它了？")

        frame = build_dialogue_control_frame(state, "我比较关心你的生活")
        self.assertEqual(count_recent_sora_question_streak(state["session"]["recent_turns"]), 2)
        self.assertEqual(frame["question_budget"], 0)
        self.assertFalse(frame["should_ask_question"])
        self.assertEqual(frame["preferred_closing"], "statement_or_light_offer")

    def test_greeting_mode_does_not_infer_from_history(self) -> None:
        state = seed_state()
        append_recent_turn(state, "user", "我比较关心你的生活")
        append_recent_turn(state, "sora", "我这边最近有个会学人说话的旧邮筒。")

        self.assertEqual(classify_input_mode("你好呀"), "greeting")
        frame = build_dialogue_control_frame(state, "你好呀")
        self.assertEqual(frame["input_mode"], "greeting")
        self.assertEqual(frame["question_budget"], 0)
        self.assertEqual(frame["preferred_closing"], "short_greeting_ack")

        turn_frame = build_turn_frame(state, "你好呀")
        self.assertEqual(turn_frame["speech_act"], "greeting_ack")
        self.assertEqual(turn_frame["context_relevance"], "low")
        self.assertIn("user_preference", turn_frame["blocked_inferences"])

    def test_greeting_mode_output_guard_removes_over_inference(self) -> None:
        frame = {"speech_act": "greeting_ack"}
        text = enforce_turn_frame("你好。看来你更喜欢先聊聊我这边的事。", frame)
        self.assertEqual(text, "你好。")

    def test_fake_llm_respects_greeting_mode(self) -> None:
        state = seed_state()
        append_recent_turn(state, "user", "我比较关心你的生活")
        append_recent_turn(state, "sora", "我这边最近有个会学人说话的旧邮筒。")
        result = AgentRunner(FakeLLMClient()).run(state, "你好呀")
        self.assertEqual(result["reply"], "你好呀，我在。")
        self.assertEqual(result["context_pack"]["dialogue_control_frame"]["input_mode"], "greeting")
        self.assertEqual(result["context_pack"]["turn_frame"]["speech_act"], "greeting_ack")
        self.assertEqual(result["context_pack"]["context_use_policy"]["recent_turns_mode"], "metadata_only")
        self.assertEqual(result["context_pack"]["session_context"]["recent_turns"], [])
        self.assertEqual(result["response_plan"]["turn_frame"]["speech_act"], "greeting_ack")
        self.assertEqual(result["response_plan"]["context_use_policy"]["recent_turns_mode"], "metadata_only")
        self.assertFalse(result["context_pack"]["initiative_window"]["allowed"])
        self.assertEqual(result["context_pack"]["arbitration_result"]["primary_mode"], "light_greeting_reply")

    def test_agent_result_exposes_question_budget(self) -> None:
        state = seed_state()
        append_recent_turn(state, "user", "最近有什么新鲜事儿吗")
        append_recent_turn(state, "sora", "我这边有个旧邮筒。你呢？")
        append_recent_turn(state, "user", "和我聊一下那个旧邮筒？")
        append_recent_turn(state, "sora", "它学人说话还挺像的。你怎么突然想起问它了？")

        result = AgentRunner(FakeLLMClient()).run(state, "我比较关心你的生活")
        self.assertEqual(result["context_pack_summary"]["dialogue_control"]["question_budget"], 0)
        self.assertEqual(result["state_diff"]["question_budget"], 0)

    def test_growth_candidate_is_persisted_and_can_be_applied(self) -> None:
        state = seed_state()
        old = state["persona"]["persona_traits"]["playfulness"]["value"]
        result = AgentRunner(FakeLLMClient()).run(state, "这个吐槽我喜欢")
        candidate = result["response_plan"]["growth_event_candidate"]
        self.assertIsNotNone(candidate)
        self.assertEqual(len(state["persona"]["growth_events"]), 1)
        self.assertEqual(state["persona"]["growth_events"][0]["status"], "pending")

        output, _ = handle_command(f"/apply-growth-event {candidate['candidate_id']}", state)
        self.assertIn('"status": "applied"', output)
        new = state["persona"]["persona_traits"]["playfulness"]["value"]
        self.assertGreater(new, old)
        self.assertEqual(state["persona"]["growth_events"][0]["status"], "applied")

    def test_growth_candidate_can_be_rejected(self) -> None:
        state = seed_state()
        result = AgentRunner(FakeLLMClient()).run(state, "哈哈，可以")
        candidate_id = result["response_plan"]["growth_event_candidate"]["candidate_id"]
        output, _ = handle_command(f"/reject-growth-event {candidate_id}", state)
        self.assertIn('"status": "rejected"', output)
        self.assertEqual(state["persona"]["growth_events"][0]["status"], "rejected")

    def test_worldops_memory_hook_can_be_committed(self) -> None:
        state = seed_state()
        handle_command("/start-micro-quest evt_starport_lamp_001", state)
        handle_command('/complete-micro-quest evt_starport_lamp_001 --choice "写一盏送回星港的灯"', state)

        output, _ = handle_command("/memory-hooks", state)
        self.assertIn("worldops_hook_", output)
        hook_id = state["worldops"]["memory_hooks"][0]["hook_id"]
        before = len(state["memory"]["items"])

        output, _ = handle_command(f"/commit-memory-hook {hook_id}", state)
        self.assertIn('"status": "committed"', output)
        self.assertEqual(len(state["memory"]["items"]), before + 1)
        self.assertEqual(state["worldops"]["memory_hooks"][0]["status"], "committed")
        self.assertEqual(state["memory"]["items"][-1]["type"], "worldops_episode")

    def test_memory_hook_can_be_rejected(self) -> None:
        state = seed_state()
        handle_command("/start-micro-quest evt_starport_lamp_001", state)
        handle_command('/complete-micro-quest evt_starport_lamp_001 --choice "写一盏送回星港的灯"', state)
        hook_id = state["worldops"]["memory_hooks"][0]["hook_id"]

        output, _ = handle_command(f"/reject-memory-hook {hook_id}", state)
        self.assertIn('"status": "rejected"', output)
        self.assertEqual(state["worldops"]["memory_hooks"][0]["status"], "rejected")

    def test_history_commands(self) -> None:
        state = seed_state()
        append_recent_turn(state, "user", "你好")
        output, _ = handle_command("/show-history", state)
        self.assertIn("短期对话历史", output)
        self.assertIn("你好", output)
        output, _ = handle_command("/clear-history", state)
        self.assertIn("已清空", output)
        self.assertEqual(state["session"]["recent_turns"], [])

    def test_placeholder_brackets_are_accepted_for_common_ids(self) -> None:
        state = seed_state()
        output, _ = handle_command("/delete-memory <mem_seed_workflow_001>", state)
        self.assertIn("mem_seed_workflow_001 -> deleted", output)
        self.assertEqual(state["memory"]["items"][0]["status"], "deleted")

        output, _ = handle_command("/activate-world-event <evt_starport_lamp_001>", state)
        self.assertIn("已激活", output)
        self.assertEqual(state["worldops"]["active_event_id"], "evt_starport_lamp_001")

        output, _ = handle_command("/set-behavior <focus_work>", state)
        self.assertIn("focus_work", output)
        self.assertEqual(state["behavior"]["current"]["preset"], "focus_work")

    def test_help_text_explains_command_purpose(self) -> None:
        state = seed_state()
        output, _ = handle_command("/help", state)
        self.assertIn("尖括号表示占位符", output)
        self.assertIn("观察与调试", output)
        self.assertIn("/prompts", output)
        self.assertIn("查看上一轮可读轨迹面板", output)
        self.assertIn("手动调整长期特质", output)
        self.assertIn("切换 mock 行为场景", output)

    def test_prompt_commands_show_registry_and_content(self) -> None:
        state = seed_state()
        output, _ = handle_command("/prompts", state)
        self.assertIn("sora_companion_v0.6", output)
        self.assertIn("system.md", output)

        output, _ = handle_command("/prompt system", state)
        self.assertIn("陪伴型数字生命回应编排器", output)
        self.assertIn("说话质感", output)

    def test_origin_commands_and_manual_set(self) -> None:
        state = seed_state()
        output, _ = handle_command("/origins", state)
        self.assertIn("origin_echo_market_wanderer", output)

        output, _ = handle_command("/set-origin origin_echo_market_wanderer", state)
        self.assertIn("回声集市游荡者", output)
        self.assertEqual(state["origin"]["template_id"], "origin_echo_market_wanderer")
        self.assertEqual(state["worldops"]["active_event_id"], "evt_echo_market_postbox_001")
        self.assertAlmostEqual(state["persona"]["persona_traits"]["playfulness"]["value"], 0.42)

        output, _ = handle_command("/origin", state)
        self.assertIn("origin_echo_market_wanderer", output)

    def test_origin_signal_participates_as_background(self) -> None:
        state = seed_state()
        handle_command("/set-origin origin_echo_market_wanderer", state)
        result = AgentRunner(FakeLLMClient()).run(state, "随便聊聊")
        sources = {signal["source"] for signal in result["context_pack"]["signals"]}
        self.assertIn("origin", sources)
        background = result["context_pack"]["arbitration_result"]["background_signals"]
        self.assertTrue(any(signal_id.startswith("sig_origin_cold_start_texture") for signal_id in background))

    def test_clean_response_removes_action_aside_but_keeps_technical_parentheses(self) -> None:
        text = clean_response("你好（轻轻笑）我们看 state_diff (debug flag)。")
        self.assertNotIn("轻轻笑", text)
        self.assertIn("(debug flag)", text)

    def test_enforce_question_budget_removes_trailing_question(self) -> None:
        frame = {"question_budget": 0}
        text = enforce_question_budget("那个旧邮筒会学人说话，听起来有点欠揍。你呢？", frame)
        self.assertEqual(text, "那个旧邮筒会学人说话，听起来有点欠揍。")

    def test_enforce_question_budget_converts_single_question_to_statement(self) -> None:
        frame = {"question_budget": 0}
        text = enforce_question_budget("要继续聊吗？", frame)
        self.assertEqual(text, "我先把这个放在这里。")

    def test_state_store_reset_in_temp_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            state = store.load()
            handle_command("/set-mood 安静", state)
            store.save(state)
            self.assertEqual(store.load()["persona"]["short_term_state"]["mood"], "安静")
            reset = store.reset()
            self.assertEqual(reset["persona"]["short_term_state"]["mood"], "平静")

    def test_credential_manager_round_trips_fake_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = CredentialManager(Path(tmp))
            manager.save_api_key("sk-test-not-real")
            self.assertEqual(manager.load_api_key(), "sk-test-not-real")

    def test_parallel_subsystem_contracts_are_exposed(self) -> None:
        state = seed_state()
        result = AgentRunner(FakeLLMClient()).run(state, "继续聊聊 Sora 的状态")
        context = result["context_pack"]
        sources = {signal["source"] for signal in context["signals"]}
        self.assertEqual(context["event_bus"]["runtime"], "pluggable_parallel_v0.6")
        self.assertGreaterEqual(len(context["signals"]), 5)
        self.assertIn("dialogue", sources)
        self.assertIn("persona", sources)
        self.assertIn("initiative", sources)
        self.assertIn("selected_signals", context["arbitration_result"])
        self.assertEqual(result["state_diff"]["subsystem_runtime"], "pluggable_parallel_v0.6")
        self.assertTrue(all(item["status"] == "ok" for item in context["subsystem_health"]))

    def test_subsystem_registry_can_replace_local_implementation(self) -> None:
        def custom_persona(context):
            return [{
                "id": f"sig_persona_external_{context.event_bus['turn_id']}",
                "source": "persona",
                "type": "external_persona",
                "priority": 0.8,
                "confidence": 0.9,
                "visibility": "active",
            }]

        registry = SubsystemRegistry([
            FunctionSubsystemAdapter("persona", custom_persona),
        ])
        result = AgentRunner(FakeLLMClient(), subsystem_registry=registry).run(seed_state(), "你好")
        signals = result["context_pack"]["signals"]
        self.assertTrue(any(signal["type"] == "external_persona" for signal in signals))
        self.assertEqual(result["context_pack"]["subsystem_health"][0]["adapter"], "FunctionSubsystemAdapter")

    def test_subsystem_failure_degrades_without_breaking_reply(self) -> None:
        def broken_memory(context):
            raise RuntimeError("memory service unavailable")

        registry = SubsystemRegistry([
            FunctionSubsystemAdapter("memory", broken_memory),
        ])
        result = AgentRunner(FakeLLMClient(), subsystem_registry=registry).run(seed_state(), "你好")
        self.assertIn("reply", result)
        health = result["context_pack"]["subsystem_health"]
        self.assertEqual(health[0]["status"], "degraded")
        self.assertIn("memory service unavailable", health[0]["detail"])

    def test_initiative_can_select_worldops_in_non_work_scene(self) -> None:
        state = seed_state()
        handle_command("/set-behavior game_companion", state)
        handle_command("/activate-world-event evt_echo_market_postbox_001", state)
        result = AgentRunner(FakeLLMClient()).run(state, "我有点无聊，随便聊聊")
        context = result["context_pack"]
        self.assertTrue(context["initiative_window"]["allowed"])
        self.assertEqual(context["arbitration_result"]["primary_mode"], "initiative_self_life_share")
        selected = context["arbitration_result"]["selected_signals"]
        self.assertTrue(any(signal_id.startswith("sig_initiative_self_life_share") for signal_id in selected))

    def test_proactive_idle_turn_emits_without_user_input(self) -> None:
        state = seed_state()
        handle_command("/activate-world-event evt_echo_market_postbox_001", state)
        result = AgentRunner(FakeLLMClient()).run_proactive(state, {
            "type": "idle_timer",
            "idle_seconds": 180,
            "fired_at": 1,
            "simulated": False,
        })
        self.assertTrue(result["emitted"])
        self.assertEqual(result["turn_kind"], "proactive")
        self.assertEqual(result["user_input"], "")
        self.assertEqual(result["context_pack"]["event_bus"]["turn_kind"], "proactive")
        self.assertEqual(result["context_pack"]["turn_frame"]["speech_act"], "proactive_opening")
        self.assertEqual(result["context_pack"]["arbitration_result"]["primary_mode"], "initiative_self_life_share")
        self.assertEqual(state["session"]["recent_turns"][-1]["role"], "sora")
        self.assertEqual(len(state["initiative"]["recent_attempts"]), 1)

    def test_proactive_idle_turn_stays_silent_during_focus_work(self) -> None:
        state = seed_state()
        handle_command("/set-behavior focus_work", state)
        result = AgentRunner(FakeLLMClient()).run_proactive(state, {
            "type": "idle_timer",
            "idle_seconds": 180,
            "fired_at": 1,
            "simulated": False,
        })
        self.assertFalse(result["emitted"])
        self.assertEqual(result["context_pack"]["arbitration_result"]["primary_mode"], "proactive_silent")
        self.assertIsNone(result["response_plan"])
        self.assertEqual(result["token_usage"], {})

    def test_proactive_idle_turn_stays_silent_during_project_design(self) -> None:
        state = seed_state()
        handle_command("/set-behavior project_design", state)
        result = AgentRunner(FakeLLMClient()).run_proactive(state, {
            "type": "idle_timer",
            "idle_seconds": 180,
            "fired_at": 1,
            "simulated": False,
        })
        self.assertFalse(result["emitted"])
        self.assertIn(
            "interruptibility=blocked",
            result["context_pack"]["initiative_window"]["reason"],
        )
        self.assertEqual(result["token_usage"], {})

    def test_proactive_idle_turn_requires_user_presence(self) -> None:
        state = seed_state()
        state["behavior"]["current"]["user_present"] = False
        result = AgentRunner(FakeLLMClient()).run_proactive(state, {
            "type": "idle_timer",
            "idle_seconds": 180,
            "fired_at": 1,
            "simulated": False,
        })
        self.assertFalse(result["emitted"])
        self.assertIn("不在设备前", result["context_pack"]["initiative_window"]["reason"])

    def test_proactive_idle_turn_requires_threshold(self) -> None:
        state = seed_state()
        result = AgentRunner(FakeLLMClient()).run_proactive(state, {
            "type": "idle_simulation",
            "idle_seconds": 30,
            "fired_at": 1,
            "simulated": True,
        })
        self.assertFalse(result["emitted"])
        self.assertIn("尚未达到 180 秒", result["context_pack"]["initiative_window"]["reason"])

    def test_proactive_idle_turn_obeys_cooldown(self) -> None:
        state = seed_state()
        runner = AgentRunner(FakeLLMClient())
        trigger = {"type": "idle_timer", "idle_seconds": 180, "fired_at": 1, "simulated": False}
        first = runner.run_proactive(state, trigger)
        second = runner.run_proactive(state, trigger)
        self.assertTrue(first["emitted"])
        self.assertFalse(second["emitted"])
        self.assertIn("冷却", second["context_pack"]["initiative_window"]["reason"])

    def test_used_proactive_worldops_topic_remains_available_when_user_asks(self) -> None:
        state = seed_state()
        handle_command("/activate-world-event evt_archive_misfiled_letter_001", state)
        runner = AgentRunner(FakeLLMClient())
        proactive = runner.run_proactive(state, {
            "type": "idle_timer",
            "idle_seconds": 180,
            "fired_at": 1,
            "simulated": False,
        })
        self.assertTrue(proactive["emitted"])
        self.assertEqual(
            state["initiative"]["proactive_topic_history"][-1]["topic_id"],
            "evt_archive_misfiled_letter_001",
        )

        reactive = runner.run(state, "那封误投的旧信后来怎么样了？")
        worldops_signals = [
            signal
            for signal in reactive["context_pack"]["signals"]
            if signal["source"] == "worldops" and signal["type"] == "world_event_share"
        ]
        self.assertEqual(len(worldops_signals), 1)
        self.assertEqual(
            worldops_signals[0]["evidence"]["event_ids"],
            ["evt_archive_misfiled_letter_001"],
        )

    def test_used_worldops_topic_is_not_repeated_as_proactive_opening(self) -> None:
        state = seed_state()
        handle_command("/activate-world-event evt_archive_misfiled_letter_001", state)
        runner = AgentRunner(FakeLLMClient())
        trigger = {"type": "idle_timer", "idle_seconds": 180, "fired_at": 1, "simulated": False}
        first = runner.run_proactive(state, trigger)
        self.assertTrue(first["emitted"])

        state["initiative"]["last_initiated_at"] = None
        second = runner.run_proactive(state, trigger)
        self.assertFalse(second["emitted"])
        self.assertEqual(
            second["context_pack"]["arbitration_result"]["primary_mode"],
            "proactive_silent",
        )

    def test_proactive_context_only_injects_recent_ten_rounds(self) -> None:
        state = seed_state()
        for idx in range(15):
            append_recent_turn(state, "user", f"u{idx}")
            append_recent_turn(state, "sora", f"s{idx}")
        result = AgentRunner(FakeLLMClient()).run_proactive(state, {
            "type": "idle_timer",
            "idle_seconds": 180,
            "fired_at": 1,
            "simulated": False,
        })
        session = result["context_pack"]["session_context"]
        self.assertEqual(session["recent_turns_mode"], "proactive_tail")
        self.assertEqual(session["history_items"], 20)
        self.assertEqual(session["history_rounds"], 10)
        self.assertEqual(session["recent_turns"][0]["content"], "u5")

    def test_idle_duration_parser(self) -> None:
        self.assertEqual(parse_duration_seconds("3m"), 180)
        self.assertEqual(parse_duration_seconds("30s"), 30)
        self.assertEqual(parse_duration_seconds("1"), 60)

    def test_proactive_scheduler_emits_idle_trigger(self) -> None:
        received = []
        ready = threading.Event()

        def callback(trigger):
            received.append(trigger)
            ready.set()

        scheduler = ProactiveScheduler(callback, idle_seconds=0, poll_seconds=0.01)
        scheduler.start()
        try:
            self.assertTrue(ready.wait(0.5))
        finally:
            scheduler.stop()
        self.assertEqual(received[0].trigger_type, "idle_timer")

    def test_idle_threshold_command(self) -> None:
        state = seed_state()
        output, _ = handle_command("/set-idle-threshold 2.5", state)
        self.assertIn("150 秒", output)
        self.assertEqual(state["initiative"]["idle_threshold_sec"], 150)

    def test_greeting_suppresses_initiative_even_in_non_work_scene(self) -> None:
        state = seed_state()
        handle_command("/set-behavior game_companion", state)
        handle_command("/activate-world-event evt_echo_market_postbox_001", state)
        result = AgentRunner(FakeLLMClient()).run(state, "你好呀")
        context = result["context_pack"]
        self.assertFalse(context["initiative_window"]["allowed"])
        suppressed = context["arbitration_result"]["suppressed_signals"]
        self.assertTrue(any(item["source"] == "initiative" for item in suppressed))

    def test_signal_debug_commands(self) -> None:
        state = seed_state()
        AgentRunner(FakeLLMClient()).run(state, "继续聊聊")
        signals, _ = handle_command("/signals", state)
        arbitration, _ = handle_command("/arbitration", state)
        initiative, _ = handle_command("/initiative", state)
        proactive, _ = handle_command("/proactive", state)
        subsystems, _ = handle_command("/subsystems", state)
        self.assertIn('"source": "dialogue"', signals)
        self.assertIn('"selected_signals"', arbitration)
        self.assertIn('"last_window"', initiative)
        self.assertIn('"idle_threshold_sec": 180', proactive)
        self.assertIn('"subsystem": "dialogue"', subsystems)


if __name__ == "__main__":
    unittest.main()
