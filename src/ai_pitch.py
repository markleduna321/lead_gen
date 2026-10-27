"""OpenAI prompt building and calls for single-lead pitch drafts.

Nothing here touches the database, so callers can run it without holding a
connection open for the length of the OpenAI requests.
"""

import requests

from niche_prompts import OUTSOURCING_SERVICE_TYPES, get_niche_parameters, get_service_parameters

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
MODEL = "gpt-4o-mini"


class PitchFailed(Exception):
    """OpenAI could not produce a draft. The message is for logs only."""


def build_prompts(lead):
    business_name = lead["business_name"]
    category = lead["category"]
    lead_type = lead.get("lead_type") or "web_design"
    if lead_type in OUTSOURCING_SERVICE_TYPES:
        strategy = get_service_parameters(lead_type)
        service = lead_type.replace("_", " ")
        system_prompt = (
            "You are a B2B outreach specialist for a Philippine-based outsourcing and digital services agency. "
            "Write a compelling cold outreach email to a company decision-maker. "
            "Be direct, professional, and concise. Zero corporate fluff or buzzword stuffing. "
            "CRITICAL: Do NOT write a subject line. Start directly with the greeting. "
            f"Service we're pitching: {service}. "
            f"Core pain points to address: {strategy['pain_points']}. "
            f"Use this terminology naturally: {strategy['buzzwords']}."
        )
        user_prompt = (
            f"Write a cold outreach email to '{business_name}', a company actively hiring for '{category}' roles.\n\n"
            f"Use this hook angle: {strategy['hook_angle']}\n\n"
            f"End with this CTA: {strategy['cta']}\n\n"
            "Keep it under 160 words. Do NOT write a subject line under any circumstances."
        )
        angle = f"[{service.title()}] {strategy['hook_angle'][:80]}..."
    else:
        strategy = get_niche_parameters(category)
        system_prompt = (
            "You are an elite B2B outreach copywriting specialist for an international web design agency. "
            "Write a highly compelling cold outreach email from the agency to a business owner. "
            "Keep the tone direct, professional, and entirely free of corporate fluff. "
            "CRITICAL: Do NOT write a subject line. Start drafting directly from the greeting. "
            f"Tailor the pitch around these industry pain points: {strategy['pain_points']}. "
            f"Weave in this terminology naturally: {strategy['buzzwords']}."
        )
        user_prompt = (
            f"Write a personalized outreach email for '{business_name}', a business in the '{category}' space.\n\n"
            f"Core Strategy Hook: {strategy['hook_angle']}\n\n"
            "Requirements:\n"
            "1. Acknowledge their strong reputation and review history.\n"
            "2. Address the digital visibility gap they're losing market share to.\n"
            "3. Conclude with a low-friction CTA for a quick 5-minute sync or free mockup.\n"
            "4. Do NOT output a subject line under any circumstances."
        )
        angle = f"[Web Design] {strategy['hook_angle'][:80]}..."
    return system_prompt, user_prompt, angle, strategy, lead_type


def _complete(api_key, messages, temperature, max_tokens, timeout):
    try:
        response = requests.post(
            OPENAI_URL,
            json={"model": MODEL, "messages": messages, "temperature": temperature, "max_tokens": max_tokens},
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise PitchFailed(f"{type(exc).__name__}: {exc}") from exc
    if response.status_code != 200:
        raise PitchFailed(f"HTTP {response.status_code}: {response.text[:500]}")
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise PitchFailed(f"Unexpected response shape: {exc}") from exc
    if not isinstance(content, str) or not content.strip():
        raise PitchFailed("Empty completion")
    return content.strip()


def draft_pitch(api_key, lead):
    """Returns (body, subject_line, angle). Raises PitchFailed if the body fails."""
    system_prompt, user_prompt, angle, strategy, lead_type = build_prompts(lead)
    body = _complete(api_key, [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ], 0.65, 420, 15)
    if "subject:" in body.lower():
        body = "\n".join(line for line in body.split("\n")
                         if not line.lower().startswith("subject:")).strip()
    if not body:
        raise PitchFailed("Draft contained only a subject line")

    business_name = lead["business_name"]
    try:
        subject_line = _complete(api_key, [
            {"role": "system", "content": (
                "Generate ONE compelling cold email subject line. "
                "Rules: Under 8 words. No clickbait. No ALL CAPS. No generic words like 'opportunity' or 'partnership'. "
                "Sound peer-to-peer, not like a mass sales blast. Output only the subject line, no quotes, no prefix."
            )},
            {"role": "user", "content": (
                f"Business: {business_name}\n"
                f"Service: {lead_type.replace('_', ' ')}\n"
                f"Hook: {strategy.get('hook_angle', '')[:100]}"
            )},
        ], 0.85, 25, 10).strip('"')
    except PitchFailed:
        subject_line = ""
    return body, subject_line or f"Quick concept for {business_name}", angle
