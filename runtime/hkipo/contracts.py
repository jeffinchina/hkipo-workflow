"""One output contract for skills, review and deterministic delivery gates."""

def obj(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}

def arr(items):
    return {"type": "array", "items": items}

S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}
REF = obj({"document_id": S, "pdf_page": I, "quote": S})
FACT = obj({
    "id": S, "key": S, "value": S, "items": arr(S), "unit": S,
    "currency": S, "period": S, "basis": S,
    "status": {"type": "string", "enum": ["supported", "unresolved", "not_disclosed", "not_applicable"]},
    "evidence": arr(REF),
})
BLOCK = obj({
    "role": {"type": "string", "enum": ["hook", "disclaimer", "heading", "paragraph", "opinion", "field", "item", "signature"]},
    "text": S, "fact_ids": arr(S),
})
COVERAGE = obj({"document_id": S, "sha256": S, "pages_total": I,
                "pages_reviewed": arr(I), "unreadable_pages": arr(I)})
CALC = obj({"id": S, "operation": {"type": "string", "enum": ["divide", "multiply", "sum", "subtract", "percent_change", "convert"]},
            "input_fact_ids": arr(S), "value": S, "unit": S, "currency": S, "basis": S})
ARTICLE_SCHEMA = obj({
    "event_id": S,
    "kind": {"type": "string", "enum": ["offer", "allotment", "exception"]},
    "sources_seen": arr(COVERAGE), "facts": arr(FACT), "calculations": arr(CALC),
    "article": obj({"title": S, "title_fact_ids": arr(S), "blocks": arr(BLOCK)}),
    "gaps": arr(S), "author_inputs_used": arr(S),
})
REVIEW_SCHEMA = obj({
    "decision": {"type": "string", "enum": ["pass", "needs_revision", "needs_source"]},
    "checked_fact_ids": arr(S),
    "title_checked": B, "unreferenced_claims_checked": B,
    "reviewed_documents": arr(COVERAGE),
    "issues": arr(obj({"severity": {"type": "string", "enum": ["blocking", "advisory"]},
                       "fact_id": S, "block_index": I, "problem": S, "source_refs": arr(REF)})),
    "summary": S,
})

# State every core field, even when the original has not disclosed it. Only
# positively supported identity/event fields are universal exception prerequisites.
# Actions still require evidence when stated; delay news need not invent an action.
CORE_FIELDS = {
    "offer": ["issuer_legal_name", "stock_code", "business", "offer_price", "offer_price_type", "lot_size", "entry_fee", "subscription_period", "listing_date", "global_offer_shares", "hong_kong_offer_shares", "reallocation", "sponsors", "cornerstones", "over_allotment", "financials"],
    "allotment": ["issuer_legal_name", "stock_code", "final_offer_price", "listing_date", "public_subscription", "international_subscription", "final_public_allocation", "final_international_allocation", "reallocation", "allotment_table", "concentration"],
    "exception": ["issuer_legal_name", "stock_code", "event_nature", "affected_previous_terms", "new_arrangement", "investor_action", "official_reason"],
}
HARD_FIELDS = {
    "offer": ["issuer_legal_name", "stock_code", "offer_price", "offer_price_type", "lot_size", "subscription_period"],
    "allotment": ["issuer_legal_name", "stock_code", "final_offer_price", "listing_date", "final_public_allocation", "final_international_allocation"],
    "exception": ["issuer_legal_name", "stock_code", "event_nature"],
}
