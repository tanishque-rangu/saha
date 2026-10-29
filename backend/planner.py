"""Saha Planner Module.

Uses PLANNER model (nvidia/Nemotron-3-Ultra-550b-a55b) to decompose high-level
goals into ordered, deterministic steps with explicit verification probes.
Validates outputs with Pydantic with automated retry on schema errors.
"""

import json
import os
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, ValidationError

from backend.llm import PLANNER_MODEL, call_llm_json

ToolType = Literal["write_file", "read_file", "run_shell", "http_get"]
MAX_PUBLIC_STEPS = 8


def is_public_mode() -> bool:
    return os.getenv("SAHA_PUBLIC_MODE", "false").lower() in ("true", "1", "yes")


class StepPlan(BaseModel):
    idx: int
    intent: str
    tool: ToolType
    args: Dict[str, Any]
    probe: Dict[str, Any] = Field(default_factory=dict)


class TaskPlan(BaseModel):
    steps: List[StepPlan]


def get_planner_system_prompt() -> str:
    public_mode = is_public_mode()
    if public_mode:
        tools_desc = """Available sandboxed tools (executed in workspace/<task_id>/):
1. write_file: {"path": str, "content_brief": str} (Brief describing what the file should contain; WORKER will generate the actual content)
2. read_file: {"path": str}
3. http_get: {"url": str}

NOTE: You are running in SAHA_PUBLIC_MODE. 'run_shell' is strictly DISABLED and forbidden."""
    else:
        tools_desc = """Available sandboxed tools (executed in workspace/<task_id>/):
1. write_file: {"path": str, "content_brief": str} (Brief describing what the file should contain; WORKER will generate the actual content)
2. read_file: {"path": str}
3. run_shell: {"cmd": str} (Allowed commands: ls, cat, head, tail, grep, echo, python <script.py>)
4. http_get: {"url": str}"""

    return f"""You are Saha's Master Planner, powered by NVIDIA Nemotron.
Your job is to break down a user's goal into an ordered sequence of executable, deterministic steps.

{tools_desc}

CRITICAL RULES:
- For `write_file`, do NOT generate the full file body in the plan. Instead provide `content_brief`: a clear prompt describing what facts, data, or code the file must contain. The WORKER model will synthesize the exact content.
- For each step, provide:
  - idx: 1-indexed sequential number
  - intent: concise description of the step's purpose
  - tool: tool name
  - args: dictionary of tool arguments
  - probe: deterministic verification criteria (e.g. expected_output, expected_file, expected_status)

Output MUST be a JSON object with a single key "steps":
{{
  "steps": [
    {{
      "idx": 1,
      "intent": "Write Hyderabad history facts",
      "tool": "write_file",
      "args": {{"path": "facts1.txt", "content_brief": "Two bullet points about the founding of Hyderabad by Muhammad Quli Qutb Shah in 1591."}},
      "probe": {{}}
    }}
  ]
}}
"""


def generate_plan(goal: str, context: Optional[str] = None, max_retries: int = 1) -> List[StepPlan]:
    """Generate an ordered execution plan from a goal using the PLANNER model."""
    user_prompt = f"Goal: {goal}"
    if context:
        user_prompt += f"\n\nContext & Current State:\n{context}"

    messages = [
        {"role": "system", "content": get_planner_system_prompt()},
        {"role": "user", "content": user_prompt},
    ]

    attempts = 0
    last_err = None
    while attempts <= max_retries:
        attempts += 1
        try:
            raw_dict = call_llm_json(
                model=PLANNER_MODEL,
                messages=messages,
                role="planner",
                temperature=0.0,
            )
            if isinstance(raw_dict, list):
                raw_dict = {"steps": raw_dict}

            plan = TaskPlan.model_validate(raw_dict)
            if not plan.steps:
                raise ValueError("Planner returned an empty step list")

            if is_public_mode() and len(plan.steps) > MAX_PUBLIC_STEPS:
                raise ValueError(
                    f"Plan has {len(plan.steps)} steps, exceeding the limit of {MAX_PUBLIC_STEPS} in public mode."
                )

            # Ensure 1-indexed sequential ordering
            for i, step in enumerate(plan.steps, start=1):
                step.idx = i
                # Public mode validation: reject run_shell
                if is_public_mode() and step.tool == "run_shell":
                    raise PermissionError("run_shell is forbidden in SAHA_PUBLIC_MODE")

            return plan.steps

        except (ValidationError, ValueError, PermissionError, json.JSONDecodeError) as e:
            last_err = e
            if attempts <= max_retries:
                messages.append({
                    "role": "user",
                    "content": f"The plan was invalid: {e}. Return ONLY valid JSON adhering strictly to the schema and constraints.",
                })

    raise ValueError(f"Failed to generate valid plan after {max_retries + 1} attempts: {last_err}")


def replan_drift(
    goal: str,
    ledger_steps: List[Dict[str, Any]],
    drift_report: Dict[str, Any],
    max_retries: int = 1,
) -> List[StepPlan]:
    """Replan only remaining and drifted work when reality diverges from the ledger."""
    prompt = f"""Goal: {goal}

Current Ledger History (previous steps and their arguments):
{json.dumps(ledger_steps, indent=2)}

Drift Report (Reality Mismatches):
{json.dumps(drift_report, indent=2)}

INSTRUCTION:
Create a revised plan containing ONLY the steps needed to restore/repair the drifted state and finish the uncompleted goal.

CRITICAL RULES FOR DRIFT REPAIR:
1. If a file was reported missing or drifted in the Drift Report, you must RE-CREATE it using `write_file` with its required `content_brief`. DO NOT use `read_file` to fix a missing file.
2. If any downstream steps (e.g. data processing or combining) depended on that restored file, re-run those downstream steps after recreating the file.
3. Do not repeat earlier steps that are still verified and intact in physical reality.
4. Output MUST be valid JSON with a "steps" list matching the schema.
"""
    messages = [
        {"role": "system", "content": get_planner_system_prompt()},
        {"role": "user", "content": prompt},
    ]
    raw_dict = call_llm_json(
        model=PLANNER_MODEL,
        messages=messages,
        role="planner",
        temperature=0.0,
    )
    if isinstance(raw_dict, list):
        raw_dict = {"steps": raw_dict}

    plan = TaskPlan.model_validate(raw_dict)
    return plan.steps
