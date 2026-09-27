"""schema.org JSON-LD builders. Output is a single @graph per page, with entity ids kept
identical across pages (entity consistency for AEO/GEO)."""

from __future__ import annotations

import json
from typing import Any

from markupsafe import Markup

from redblue.cms.blocks import blocks_text


def dumps_for_script(data: Any) -> Markup:
    """JSON safe to embed in <script type="application/ld+json"> (no </script> breakout)."""
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    raw = raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    raw = raw.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return Markup(raw)  # noqa: S704  # nosec B704 - JSON with HTML-significant characters escaped


def organization(site: dict) -> dict:
    org = {
        "@type": "Organization",
        "@id": f"{site['base_url']}/#organization",
        "name": site.get("organization_name") or site["site_name"],
        "url": site["base_url"],
    }
    if site.get("organization_logo"):
        org["logo"] = site["organization_logo"]
    return org


def website(site: dict) -> dict:
    return {
        "@type": "WebSite",
        "@id": f"{site['base_url']}/#website",
        "url": site["base_url"],
        "name": site["site_name"],
        "publisher": {"@id": f"{site['base_url']}/#organization"},
        "potentialAction": {
            "@type": "SearchAction",
            "target": f"{site['base_url']}/search?q={{search_term_string}}",
            "query-input": "required name=search_term_string",
        },
    }


def person(site: dict, author: dict | None) -> dict | None:
    if not author:
        return None
    p = {
        "@type": "Person",
        "@id": f"{site['base_url']}/authors/{author['id']}#person",
        "name": author["name"],
        "url": f"{site['base_url']}/authors/{author['id']}",
    }
    if author.get("credentials"):
        p["jobTitle"] = author["credentials"]
    return p


def breadcrumbs(site: dict, crumbs: list[tuple[str, str]]) -> dict | None:
    if len(crumbs) < 2:
        return None
    return {
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": name, "item": site["base_url"] + path}
            for i, (name, path) in enumerate(crumbs)
        ],
    }


def _blocks_of(blocks: list[dict], t: str) -> list[dict]:
    return [b for b in blocks if b.get("type") == t]


def faq_page(blocks: list[dict]) -> dict | None:
    qas = [qa for b in _blocks_of(blocks, "faq") for qa in b.get("items", [])]
    if not qas:
        return None
    return {
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": q["question"],
                "acceptedAnswer": {"@type": "Answer", "text": q["answer"]},
            }
            for q in qas
        ],
    }


def howto(blocks: list[dict]) -> dict | None:
    hs = _blocks_of(blocks, "howto")
    if not hs:
        return None
    h = hs[0]
    out = {
        "@type": "HowTo",
        "name": h["name"],
        "step": [
            {"@type": "HowToStep", "position": i + 1, "name": s["name"], "text": s["text"]}
            for i, s in enumerate(h["steps"])
        ],
    }
    if h.get("total_time"):
        out["totalTime"] = h["total_time"]
    if h.get("supplies"):
        out["supply"] = [{"@type": "HowToSupply", "name": s} for s in h["supplies"]]
    return out


def qa_page(title: str, blocks: list[dict]) -> dict | None:
    ans = _blocks_of(blocks, "answer")
    if not ans:
        return None
    a = ans[0]
    return {
        "@type": "QAPage",
        "mainEntity": {
            "@type": "Question",
            "name": a["question"],
            "answerCount": 1,
            "acceptedAnswer": {
                "@type": "Answer",
                "text": a["answer"] + (" " + a["detail"] if a.get("detail") else ""),
            },
        },
    }


def verified_reviews(blocks: list[dict]) -> list[dict]:
    """Only editor-verified testimonials with a rating become Review markup."""
    return [
        {
            "@type": "Review",
            "reviewBody": b["quote"],
            "author": {"@type": "Person", "name": b["author"]},
            "reviewRating": {"@type": "Rating", "ratingValue": b["rating"], "bestRating": 5},
        }
        for b in _blocks_of(blocks, "testimonial")
        if b.get("verified") and b.get("rating")
    ]


def page_graph(site: dict, page: dict) -> dict:
    """``page`` keys: schema_type, url, title, description, blocks, data, published,
    modified, author, crumbs, image, collection."""
    base = site["base_url"]
    blocks = page.get("blocks", [])
    data = page.get("data", {})
    st = page.get("schema_type") or "WebPage"
    url = base + page["url"]
    author = person(site, page.get("author"))
    graph: list[dict] = [organization(site), website(site)]
    node: dict[str, Any] = {"@id": f"{url}#main", "url": url, "name": page["title"]}
    if page.get("description"):
        node["description"] = page["description"]
    if page.get("image"):
        node["image"] = base + page["image"] if page["image"].startswith("/") else page["image"]

    article_like = {"Article", "BlogPosting", "NewsArticle", "TechArticle"}
    if st in article_like:
        node.update(
            {
                "@type": st,
                "headline": page["title"][:110],
                "publisher": {"@id": f"{base}/#organization"},
                "mainEntityOfPage": url,
            }
        )
        if page.get("published"):
            node["datePublished"] = page["published"]
        if page.get("modified"):
            node["dateModified"] = page["modified"]
        if author:
            node["author"] = {"@id": author["@id"]}
        node["wordCount"] = len(blocks_text(blocks).split())
    elif st == "Product":
        node["@type"] = "Product"
        if data.get("brand"):
            node["brand"] = {"@type": "Brand", "name": data["brand"]}
        if data.get("sku"):
            node["sku"] = data["sku"]
        if data.get("price"):
            node["offers"] = {
                "@type": "Offer",
                "price": data["price"],
                "priceCurrency": data.get("currency", "USD"),
                "availability": f"https://schema.org/{data.get('availability', 'InStock')}",
                "url": url,
            }
        reviews = verified_reviews(blocks)
        if reviews:
            node["review"] = reviews
            node["aggregateRating"] = {
                "@type": "AggregateRating",
                "reviewCount": len(reviews),
                "ratingValue": round(
                    sum(r["reviewRating"]["ratingValue"] for r in reviews) / len(reviews), 1
                ),
            }
    elif st in ("LocalBusiness", "Service"):
        node["@type"] = st
        addr = {
            k: data.get(src)
            for k, src in (
                ("streetAddress", "street"),
                ("addressLocality", "city"),
                ("addressRegion", "region"),
                ("postalCode", "postal_code"),
                ("addressCountry", "country"),
            )
            if data.get(src)
        }
        if st == "LocalBusiness":
            node["name"] = data.get("name") or page["title"]
            if addr:
                node["address"] = {"@type": "PostalAddress", **addr}
            if data.get("phone"):
                node["telephone"] = data["phone"]
            if data.get("latitude") is not None and data.get("longitude") is not None:
                node["geo"] = {
                    "@type": "GeoCoordinates",
                    "latitude": data["latitude"],
                    "longitude": data["longitude"],
                }
            if data.get("opening_hours"):
                node["openingHours"] = data["opening_hours"]
            node["parentOrganization"] = {"@id": f"{base}/#organization"}
        else:
            node["provider"] = {"@id": f"{base}/#organization"}
            areas = data.get("service_areas") or [data.get("city")]
            node["areaServed"] = [a for a in areas if a]
    elif st == "JobPosting":
        node.update(
            {
                "@type": "JobPosting",
                "title": page["title"],
                "description": page.get("html_description") or blocks_text(blocks),
                "datePosted": page.get("published"),
                "employmentType": data.get("employment_type", "FULL_TIME"),
                "hiringOrganization": {"@id": f"{base}/#organization"},
            }
        )
        if data.get("valid_through"):
            node["validThrough"] = data["valid_through"]
        if data.get("remote"):
            node["jobLocationType"] = "TELECOMMUTE"
        else:
            node["jobLocation"] = {"@type": "Place", "address": data.get("location")}
        if data.get("salary_min"):
            node["baseSalary"] = {
                "@type": "MonetaryAmount",
                "currency": data.get("currency", "USD"),
                "value": {
                    "@type": "QuantitativeValue",
                    "minValue": data["salary_min"],
                    "maxValue": data.get("salary_max") or data["salary_min"],
                    "unitText": "YEAR",
                },
            }
    elif st == "Event":
        node.update(
            {
                "@type": "Event",
                "startDate": data.get("start"),
                "organizer": {"@id": f"{base}/#organization"},
                "eventAttendanceMode": "https://schema.org/"
                + (
                    "OnlineEventAttendanceMode"
                    if data.get("online")
                    else "OfflineEventAttendanceMode"
                ),
            }
        )
        if data.get("end"):
            node["endDate"] = data["end"]
        node["location"] = (
            {"@type": "VirtualLocation", "url": url}
            if data.get("online")
            else {"@type": "Place", "name": data.get("location") or ""}
        )
    elif st == "DefinedTerm":
        node.update(
            {
                "@type": "DefinedTerm",
                "description": data.get("short_definition") or page.get("description"),
                "inDefinedTermSet": f"{base}/glossary",
            }
        )
    elif st == "SoftwareApplication":
        node.update({"@type": "SoftwareApplication", "applicationCategory": "BusinessApplication"})
    else:
        node["@type"] = st if st not in ("HowTo", "QAPage", "FAQPage") else "WebPage"
        node["isPartOf"] = {"@id": f"{base}/#website"}
        if page.get("modified"):
            node["dateModified"] = page["modified"]
    graph.append(node)
    if author:
        graph.append(author)
    for extra in (
        faq_page(blocks),
        howto(blocks) if st == "HowTo" or _blocks_of(blocks, "howto") else None,
        qa_page(page["title"], blocks) if st == "QAPage" else None,
        breadcrumbs(site, page.get("crumbs", [])),
    ):
        if extra:
            graph.append(extra)
    return {"@context": "https://schema.org", "@graph": graph}


REQUIRED_PROPS: dict[str, tuple[str, ...]] = {
    "Article": ("headline", "datePublished", "publisher"),
    "BlogPosting": ("headline", "datePublished", "publisher"),
    "TechArticle": ("headline", "publisher"),
    "Product": ("name",),
    "JobPosting": ("title", "description", "datePosted", "hiringOrganization"),
    "Event": ("name", "startDate", "location"),
    "LocalBusiness": ("name", "address"),
    "FAQPage": ("mainEntity",),
    "HowTo": ("name", "step"),
    "QAPage": ("mainEntity",),
    "BreadcrumbList": ("itemListElement",),
    "Organization": ("name", "url"),
}


def validate_graph(doc: dict) -> list[str]:
    """Lightweight structural validation used in CI and the SEO audit."""
    problems: list[str] = []
    if doc.get("@context") != "https://schema.org":
        problems.append("Missing @context https://schema.org")
    for node in doc.get("@graph", []):
        t = node.get("@type")
        if not t:
            problems.append("Node without @type")
            continue
        for prop in REQUIRED_PROPS.get(t, ()):
            if node.get(prop) in (None, "", []):
                problems.append(f"{t} is missing required property {prop!r}")
    return problems
