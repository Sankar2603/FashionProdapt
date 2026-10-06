"""Catalogue admin: changes go through the same signed webhook a real source system would use."""

import streamlit as st

import api

st.set_page_config(page_title="Admin · FashionRec", page_icon="🛠️", layout="wide")
st.title("🛠️ Catalogue admin")
st.caption("Every action sends a signed webhook to the Catalog service. The worker syncs it "
           "into search within about a second, so run the same search again to see the change.")


def load(asin: str) -> None:
    st.session_state.admin_error = None
    try:
        st.session_state.admin_product = api.get_product(asin)
        st.session_state.admin_missing = st.session_state.admin_product is None
    except api.ApiError as err:
        st.session_state.admin_product = None
        st.session_state.admin_error = f"Catalog service error: {err.message}"


def run(action, *args, **kwargs) -> None:
    """Send one webhook, remember the response, then reload the product."""
    try:
        st.session_state.admin_result = action(*args, **kwargs)
    except api.ApiError as err:
        st.session_state.admin_result = None
        st.session_state.admin_error = f"Webhook failed ({err.code}): {err.message}"
        return
    asin = st.session_state.admin_result["parent_asin"]
    st.session_state.pending_asin = asin     # shown in the ASIN box after the rerun
    load(asin)


def rating_inputs(prefix: str, current: dict | None = None) -> dict:
    """Simulated review totals, as a store platform would report them."""
    current = current or {}
    left, right = st.columns(2)
    rating_number = left.number_input("Number of star ratings", min_value=0, step=10,
                                      value=current.get("rating_number") or 0,
                                      key=f"{prefix}_rn",
                                      help=f"Drives the Bayesian score. At {api.BAYES_M:g} "
                                           "ratings, the product's own rating counts for half.")
    average_rating = right.slider("Average star rating", 1.0, 5.0, step=0.1,
                                  value=float(current.get("average_rating") or 4.0),
                                  key=f"{prefix}_ar", help="Ignored when there are 0 ratings.")
    left, right = st.columns(2)
    review_count = left.number_input("Number of written reviews", min_value=0, step=10,
                                     value=current.get("review_count") or 0,
                                     key=f"{prefix}_rc", help="Stored and shown; not used in ranking.")
    avg_review_rating = right.slider("Average review rating", 1.0, 5.0, step=0.1,
                                     value=float(current.get("avg_review_rating") or 4.0),
                                     key=f"{prefix}_arr", help="Ignored when there are 0 reviews.")
    return {"average_rating": average_rating, "rating_number": int(rating_number),
            "review_count": int(review_count), "avg_review_rating": avg_review_rating}


# A widget's value can only be set before it is drawn, so apply it here.
if st.session_state.get("pending_asin"):
    st.session_state.admin_asin = st.session_state.pop("pending_asin")

# ------------------------------------------------------------------ lookup
with st.form("lookup"):
    left, right = st.columns([4, 1])
    asin = left.text_input("ASIN", key="admin_asin",
                           placeholder="Copy one from a search result, e.g. B08Y8QVR7R")
    right.write("")
    if right.form_submit_button("Look up", width="stretch") and asin.strip():
        st.session_state.admin_result = None
        load(asin.strip())

if st.session_state.get("admin_error"):
    st.error(st.session_state.admin_error)

# Result of the last action.
result = st.session_state.get("admin_result")
if result:
    status = result["status"]
    show = st.success if status == "applied" else st.info
    show(f"**{status}** · change: `{result.get('change')}` · queued for sync: "
         f"`{result.get('queued')}`\n\n{api.explain(result)}")
    with st.expander("Raw webhook response"):
        st.json(result)

product = st.session_state.get("admin_product")
if st.session_state.get("admin_missing") and not product:
    st.warning("No product with that ASIN. You can create a new one below.")

# ------------------------------------------------------- current product
if product:
    st.divider()
    left, right = st.columns([1, 3])
    if product.get("image_url"):
        left.image(product["image_url"], width=200)
    else:
        left.info("No image")
    with right:
        badge = " 🗑️ **DELETED**" if product["is_deleted"] else ""
        st.subheader(product["title"])
        st.markdown(f"`{product['parent_asin']}`{badge}")
        a, b, c, d = st.columns(4)
        a.metric("Price", f"${product['price']:.2f}")
        rating = product.get("average_rating")
        b.metric("Rating", f"{rating:.1f} ★" if rating is not None else "n/a",
                 f"{product['rating_number']:,} ratings", delta_color="off")
        d.metric("Bayesian score", f"{product['bayesian_score']:.2f}",
                 "catalogue mean (no ratings)" if not product["rating_number"] else None,
                 delta_color="off")
        c.metric("Brand", product.get("store") or "n/a")
        st.caption(f"Updated {product['updated_at']} · created {product['created_at']} · "
                   f"Bayesian score = (v/(v+{api.BAYES_M:g}))·rating + "
                   f"({api.BAYES_M:g}/(v+{api.BAYES_M:g}))·catalogue mean, v = number of ratings")
    with st.expander("Description and features"):
        st.write(product.get("description") or "_No description_")
        st.write(product.get("features") or "_No features_")

    price_tab, edit_tab, ratings_tab, delete_tab = st.tabs(
        ["💲 Change price", "✏️ Edit text", "⭐ Ratings (simulated)", "🗑️ Delete / restore"])
    with price_tab:
        with st.form("price"):
            new_price = st.number_input("New price ($)", min_value=0.01,
                                        value=float(product["price"]), step=1.0)
            if st.form_submit_button("Update price", type="primary"):
                run(api.update_price, product["parent_asin"], new_price)
                st.rerun()

    with edit_tab:
        with st.form("edit"):
            title = st.text_input("Title", value=product["title"])
            description = st.text_area("Description", value=product.get("description") or "",
                                       height=150)
            if st.form_submit_button("Save text", type="primary"):
                if not title.strip():
                    st.warning("Title can't be empty.")
                else:
                    run(api.edit_product, product["parent_asin"], title.strip(), description)
                    st.rerun()

    with ratings_tab:
        if not product["parent_asin"].startswith(api.TEST_PREFIX):
            st.info("Real catalogue products keep their real Amazon ratings. Simulated ratings "
                    f"are only for demo products (`{api.TEST_PREFIX}…` ASINs).")
        else:
            st.caption("Simulates the store platform reporting new review totals. "
                       "Expect `metadata_only`: the score changes, no re-embedding.")
            with st.form("ratings"):
                ratings = rating_inputs("edit", product)
                if st.form_submit_button("Update ratings", type="primary"):
                    run(api.set_ratings, product["parent_asin"], **ratings)
                    st.rerun()

    with delete_tab:
        if product["is_deleted"]:
            st.write("This product is deleted and hidden from search.")
            if st.button("Restore product", type="primary"):
                run(api.restore_product, product["parent_asin"])
                st.rerun()
        else:
            sure = st.checkbox("Yes, remove this product from search")
            if st.button("Delete product", type="primary", disabled=not sure):
                run(api.delete_product, product["parent_asin"])
                st.rerun()

# ------------------------------------------------------------------ create
st.divider()
with st.expander("➕ Create a new product", expanded=not product):
    with st.form("create"):
        title = st.text_input("Title", placeholder="Women's Linen Midi Dress")
        left, mid, right = st.columns(3)
        price = left.number_input("Price ($)", min_value=0.01, value=34.50, step=1.0)
        store = mid.text_input("Brand", placeholder="Demo Brand")
        category = right.text_input("Category", placeholder="Women, Dresses")
        image_url = st.text_input("Image URL", placeholder="https://… (optional)")
        description = st.text_area("Description",
                                   placeholder="Breathable linen midi dress for summer…")
        features = st.text_area("Features", placeholder="100% linen; Machine washable")
        st.caption("Title, brand, category, features and description are embedded for search: "
                   "the more you fill in, the better the product matches queries.")

        st.markdown("**Ratings (simulated)**: what the store platform would report. "
                    "Leave at 0 for a brand-new product (it gets the catalogue mean score).")
        ratings = rating_inputs("create")

        if st.form_submit_button("Create", type="primary"):
            if not title.strip():
                st.warning("A title is required.")
            else:
                run(api.create_product, title.strip(), price, store=store.strip(),
                    category=category.strip(), image_url=image_url.strip(),
                    description=description.strip(), features=features.strip(), **ratings)
                st.rerun()
    st.caption("A random `TEST…` ASIN is generated. Search for the title to see it appear.")
