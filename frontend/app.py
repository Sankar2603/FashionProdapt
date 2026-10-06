"""Search page (home). Run from the project root:  streamlit run frontend/app.py"""

import html

import streamlit as st

import api

st.set_page_config(page_title="FashionRec search", page_icon="👗", layout="wide")

EXAMPLES = [
    "black leather jacket for men",
    "camisa de lino para el verano por menos de $40",
    "महिलाओं के लिए सूती कुर्ती 30 डॉलर से कम",
    "running shoes for women under $50",
]


def use_example(text: str) -> None:
    # Runs before the widgets are drawn, so it may set the text box's value.
    st.session_state.query = text
    st.session_state.run_search = True


def stars(rating: float | None) -> str:
    if rating is None:
        return "No ratings"
    full = round(rating)
    return "★" * full + "☆" * (5 - full) + f" {rating:.1f}"


def image_html(url: str | None) -> str:
    # Fixed-height box so every card in the grid lines up.
    box = "height:220px;display:flex;align-items:center;justify-content:center;" \
          "background:#f4f4f5;border-radius:8px;overflow:hidden;"
    if not url:
        return f'<div style="{box}color:#888;">No image</div>'
    return (f'<div style="{box}"><img src="{html.escape(url)}" '
            f'style="max-height:220px;max-width:100%;object-fit:contain;"></div>')


def show_error(err: api.ApiError) -> None:
    if err.code == "rate_limited":
        wait = f" Try again in {err.retry_after} s." if err.retry_after else ""
        st.warning(f"Too many searches (30 per minute).{wait}")
    elif err.code == "retrieval_unavailable":
        st.error("Search is unavailable right now (the retrieval service is down).")
    elif err.code == "timeout":
        st.error("The search took longer than 30 s. The reranker may be overloaded; try again.")
    elif err.code == "unreachable":
        st.error("Can't reach the gateway. Is `docker compose up` running?")
    elif err.code == "validation_error":
        st.warning("Please enter a query between 1 and 500 characters.")
    else:
        st.error(f"Search failed: {err.message}")
    if err.correlation_id:
        st.caption(f"Correlation ID: `{err.correlation_id}`")


st.title("👗 FashionRec")

st.session_state.setdefault("query", "")
st.session_state.setdefault("run_search", False)

st.write("Try an example:")
cols = st.columns(len(EXAMPLES))
for col, text in zip(cols, EXAMPLES):
    col.button(text, on_click=use_example, args=(text,), width="stretch")

with st.form("search"):
    left, right = st.columns([5, 1])
    left.text_input("What are you looking for?", key="query", max_chars=500,
                    placeholder="e.g. linen summer shirt under $40")
    top_n = right.number_input("Results", min_value=1, max_value=20, value=6)
    submitted = st.form_submit_button("Search", type="primary")

if submitted or st.session_state.run_search:
    st.session_state.run_search = False
    query = st.session_state.query.strip()
    if not query:
        st.warning("Type something to search for.")
    else:
        with st.spinner("Searching… (the CPU reranker takes ~8-10 s)"):
            try:
                st.session_state.last_search = api.search(query, int(top_n))
            except api.ApiError as err:
                show_error(err)

result = st.session_state.get("last_search")
if result:
    st.divider()

    # Fallback banner: the search worked, but some stage was skipped.
    for code in result.get("degraded", []):
        st.warning(api.DEGRADED_EXPLANATIONS.get(code, f"Degraded: {code}"))

    intent = result["intent"]
    budget = f" · under ${intent['max_price']:g}" if intent.get("max_price") is not None else ""
    parser = "LLM" if intent.get("source") == "llm" else "rules fallback"
    st.markdown(f"**Understood as:** *{intent['search_query']}*{budget} · "
                f"language `{intent.get('language', '?')}` · parsed by {parser}")
    cached = " · served from cache" if result.get("cached") else ""
    st.caption(f"{result['count']} results for “{result['query']}”{cached} · "
               f"correlation ID `{result['correlation_id']}`")

    if not result["results"]:
        st.info("No products matched. Try a broader query or a higher budget.")

    # Product grid, 3 per row.
    per_row = 3
    products = result["results"]
    for start in range(0, len(products), per_row):
        row = st.columns(per_row)
        for col, p in zip(row, products[start:start + per_row]):
            with col.container(border=True):
                st.markdown(image_html(p.get("image_url")), unsafe_allow_html=True)
                st.markdown(f"**{p['title']}**")
                st.caption(p.get("store") or "Unknown brand")
                price = f"${p['price']:.2f}" if p.get("price") is not None else "No price"
                count = f" ({p['rating_number']:,})" if p.get("rating_number") else ""
                st.markdown(f"**{price}** · {stars(p.get('average_rating'))}{count}")
                if p.get("relevance") is not None:
                    st.progress(min(max(p["relevance"], 0.0), 1.0),
                                text=f"Relevance {p['relevance']:.2f}")
                st.code(p["parent_asin"], language=None)
