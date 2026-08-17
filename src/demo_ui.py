"""Interactive Streamlit demo for the Lab 17 student memory implementation."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st

from src.config import settings
from src.llm import gemini_available, generate_reply
from src.memory_student import StudentMemory
from src.router import route_query
from src.short_term import ShortTermMemory
from src.utils import GOLDEN_PATH, load_dataset, load_json
from src.zep_common import get_zep_client

LAYER_ORDER = ("short_term", "long_term", "episodic", "semantic")
LAYER_ICON = {
    "short_term": ":material/history:",
    "long_term": ":material/person:",
    "episodic": ":material/route:",
    "semantic": ":material/account_tree:",
}


@st.cache_data(ttl="10m", max_entries=2)
def load_cases() -> list[dict[str, Any]]:
    cases = list(load_dataset()["evaluations"])
    if GOLDEN_PATH.exists():
        try:
            cases.extend(load_json(GOLDEN_PATH).get("evaluations") or [])
        except Exception:
            pass
    return cases


@st.cache_data(ttl="10m", max_entries=2)
def load_user_sessions() -> dict[tuple[str, str], list[dict[str, str]]]:
    sessions: dict[tuple[str, str], list[dict[str, str]]] = {}
    for user in load_dataset()["users"]:
        for session in user.get("sessions", []):
            key = (user["user_id"], session["thread_id"])
            sessions[key] = [
                {"role": message["role"], "content": message["content"]}
                for message in session.get("messages", [])
            ]
    return sessions


@st.cache_resource
def get_memory() -> StudentMemory:
    return StudentMemory(get_zep_client())


def init_state() -> None:
    st.session_state.setdefault("active_case_id", None)
    st.session_state.setdefault("chat_messages", [])
    st.session_state.setdefault("last_result", None)


def format_case(case: dict[str, Any]) -> str:
    return f"{case['id']} | {case['expected_layer']} | {case['user_id']}"


def short_term_context(case: dict[str, Any], extra_messages: list[dict[str, str]]) -> str:
    source = case.get("fixture_messages")
    if source is None:
        source = load_user_sessions().get((case["user_id"], case["thread_id"]), [])

    memory = ShortTermMemory(strategy="sliding", max_recent_messages=6)
    for message in [*source, *extra_messages]:
        role = message.get("role", "user")
        content = message.get("content", "").strip()
        if content:
            memory.add(role, content)
    return memory.render()


def selected_layers(case: dict[str, Any], extra_messages: list[dict[str, str]]) -> set[str]:
    expected = case["expected_layer"]
    if extra_messages:
        return set(route_query(case["query"])) | {"short_term"}
    if expected == "mixed":
        return set(case.get("retrieve_layers") or ["long_term", "semantic"])
    return {expected}


def retrieve_for_case(
    memory: StudentMemory,
    case: dict[str, Any],
    extra_messages: list[dict[str, str]],
) -> dict[str, Any]:
    """Retrieve evidence for a case and assemble it with the teaching budget."""
    wanted = selected_layers(case, extra_messages)
    layers = {layer: "" for layer in LAYER_ORDER}

    if "short_term" in wanted:
        layers["short_term"] = short_term_context(case, extra_messages)
    if "long_term" in wanted:
        layers["long_term"] = memory.retrieve_long_term(
            user_id=case["user_id"],
            thread_id=case["thread_id"],
            query=case["query"],
        )
    if "episodic" in wanted:
        layers["episodic"] = memory.retrieve_episodic(
            user_id=case["user_id"],
            query=case["query"],
        )
    if "semantic" in wanted:
        layers["semantic"] = memory.retrieve_semantic(
            graph_id=settings.semantic_graph_id,
            query=case["query"],
        )

    merged_context, budget = memory.assemble_context(layers)
    return {"layers": layers, "merged_context": merged_context, "budget": budget}


def run_retrieval(case: dict[str, Any], extra_messages: list[dict[str, str]]) -> None:
    with st.status("Retrieving memory layers", expanded=True, type="compact") as status:
        st.write("Resolving scoped evidence and applying the context budget.")
        result = retrieve_for_case(get_memory(), case, extra_messages)
        active = [layer for layer, text in result["layers"].items() if text.strip()]
        st.write("Retrieved: " + (", ".join(active) if active else "no evidence"))
        status.update(label="Retrieval complete", state="complete", expanded=False)
    st.session_state.last_result = result


def render_case_details(case: dict[str, Any]) -> None:
    st.subheader("Selected evaluation")
    with st.container(border=True):
        st.markdown(f"### {case['id']}  :blue-badge[{case['expected_layer']}]")
        st.write(case["query"])
        meta_a, meta_b, meta_c = st.columns(3)
        meta_a.caption("User scope")
        meta_a.code(case["user_id"], language=None)
        meta_b.caption("Thread")
        meta_b.code(case["thread_id"], language=None)
        meta_c.caption("Expected evidence")
        meta_c.code(", ".join(case.get("must_contain_all", [])) or "-", language=None)
        if case.get("description"):
            st.caption(case["description"])


def render_budget(result: dict[str, Any]) -> None:
    st.subheader("Context budget")
    with st.container(horizontal=True, gap="small"):
        for layer in LAYER_ORDER:
            metrics = result["budget"].get(layer, {})
            st.metric(
                layer.replace("_", " "),
                f"{metrics.get('used_tokens', 0)} tok",
                f"limit {metrics.get('limit_tokens', 0)}",
                border=True,
            )


def render_retrieval(result: dict[str, Any]) -> None:
    render_budget(result)
    left, right = st.columns((1.15, 1), gap="medium")

    with left:
        st.subheader("Merged context")
        with st.container(border=True, height=360):
            st.code(result["merged_context"] or "(No context returned.)", language="markdown")

    with right:
        st.subheader("Layer evidence")
        active = [layer for layer, text in result["layers"].items() if text.strip()]
        if not active:
            st.info("No evidence was returned for this request.", icon=":material/info:")
            return
        tabs = st.tabs([layer.replace("_", " ") for layer in active])
        for tab, layer in zip(tabs, active):
            with tab:
                st.caption(f"{LAYER_ICON[layer]} {layer.replace('_', ' ')}")
                st.code(result["layers"][layer], language="markdown")


def fallback_reply(context: str) -> str:
    return (
        "Gemini is not configured. The retrieved memory context is shown below.\n\n"
        + (context[:1800] or "(No memory context returned.)")
    )


def render_chat(case: dict[str, Any]) -> None:
    st.subheader("Continue as this user")
    st.caption("Messages stay within the selected user and thread. Each turn retrieves memory again.")

    history = st.container(height=340, border=True, key="chat_history")
    with history:
        if not st.session_state.chat_messages:
            st.caption("Start with a follow-up question. Retrieval will rerun for this user and thread.")
        for message in st.session_state.chat_messages:
            avatar = ":material/person:" if message["role"] == "user" else ":material/psychology:"
            with st.chat_message(message["role"], avatar=avatar):
                st.write(message["content"])

    prompt = st.chat_input(
        "Ask a follow-up for this user and thread",
        key="memory_chat_input",
        submit_mode="disable",
    )
    if not prompt:
        return

    st.session_state.chat_messages.append({"role": "user", "content": prompt})
    with history:
        with st.chat_message("user", avatar=":material/person:"):
            st.write(prompt)

    follow_case = {**case, "query": prompt}
    try:
        with st.status("Refreshing memory for this turn", expanded=True, type="compact") as status:
            follow = retrieve_for_case(get_memory(), follow_case, st.session_state.chat_messages)
            st.session_state.last_result = follow
            status.update(label="Memory refreshed", state="complete", expanded=False)

        if gemini_available():
            reply = generate_reply(
                follow["merged_context"],
                st.session_state.chat_messages[:-1],
                prompt,
            )
        else:
            reply = fallback_reply(follow["merged_context"])

        st.session_state.chat_messages.append({"role": "assistant", "content": reply})
        with history:
            with st.chat_message("assistant", avatar=":material/psychology:"):
                st.write(reply)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Chat retrieval failed: {exc}", icon=":material/error:")


def main() -> None:
    st.set_page_config(
        page_title="Memory console",
        page_icon=":material/psychology:",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    init_state()

    cases = load_cases()
    if not cases:
        st.error("No evaluation cases were found.", icon=":material/error:")
        return

    with st.sidebar:
        st.title("Memory console")
        st.caption("Lab 17 | Zep multi-memory agent")

        if settings.zep_api_key:
            st.badge("Zep connected", icon=":material/check_circle:", color="green")
        else:
            st.badge("Zep key missing", icon=":material/error:", color="red")

        if gemini_available():
            st.badge("Gemini chat ready", icon=":material/auto_awesome:", color="blue")
        else:
            st.badge("Retrieval-only chat", icon=":material/info:", color="orange")

        st.space("small")
        layer_filter = st.multiselect(
            "Filter by memory layer",
            options=list(LAYER_ORDER) + ["mixed"],
            default=list(LAYER_ORDER) + ["mixed"],
            key="layer_filter",
        )
        visible_cases = [case for case in cases if case["expected_layer"] in layer_filter]
        if not visible_cases:
            st.warning("Select at least one memory layer.", icon=":material/filter_alt_off:")
            return

        labels = [format_case(case) for case in visible_cases]
        selected_label = st.selectbox("Evaluation case", labels, key="case_picker")
        case = visible_cases[labels.index(selected_label)]

        st.space("small")
        st.caption(f"{len(visible_cases)} cases available")
        st.caption(f"Semantic graph: {settings.semantic_graph_id}")

    if st.session_state.active_case_id != case["id"]:
        st.session_state.active_case_id = case["id"]
        st.session_state.chat_messages = []
        st.session_state.last_result = None

    header_left, header_right = st.columns((4, 1), vertical_alignment="bottom")
    with header_left:
        st.title("Memory agent demo")
        st.caption("Inspect scoped retrieval, context trimming, and a grounded follow-up conversation.")
    with header_right:
        if st.button(
            "Reset chat",
            icon=":material/delete_sweep:",
            key="reset_chat",
            width="stretch",
        ):
            st.session_state.chat_messages = []
            st.toast("Chat history cleared.", icon=":material/check_circle:")

    render_case_details(case)

    with st.container(horizontal=True, gap="small"):
        run_clicked = st.button(
            "Run retrieval",
            type="primary",
            icon=":material/play_arrow:",
            key="run_retrieval",
        )
        st.caption("The UI calls the student implementation and displays budgeted evidence.")

    if run_clicked:
        try:
            run_retrieval(case, [])
        except Exception as exc:  # noqa: BLE001
            st.error(f"Retrieval failed: {exc}", icon=":material/error:")

    result = st.session_state.last_result
    if result:
        render_retrieval(result)
    else:
        st.info(
            "Run retrieval to inspect the selected case before starting a follow-up chat.",
            icon=":material/play_circle:",
        )

    render_chat(case)


if __name__ == "__main__":
    main()