NICHE_STRATEGY_MATRIX = {
    "roofing contractor": {
        "pain_points": "Losing high-ticket residential replacements to storm-chasing competitors with active landing pages; lack of localized, high-resolution before/after galleries.",
        "buzzwords": "hail damage restoration, certified architectural shingles, insurance claim documentation, localized commercial bid positioning",
        "hook_angle": "Showcase how a fast mobile landing page captures immediate emergency replacement calls right after weather events before local competition drops physical flyers."
    },
    "hvac contractor": {
        "pain_points": "Extreme seasonal lead drop-off; missing emergency system failure calls to aggregators like Yelp; zero visibility for high-margin preventative maintenance agreements.",
        "buzzwords": "seasonal system tune-ups, SEER2 efficiency retrofits, commercial load calculations, 24/7 emergency dispatch optimization",
        "hook_angle": "Position an automated emergency dispatch form that captures immediate off-hours AC/heating failure leaks and pushes high-margin maintenance contracts."
    },
    "plumbing service": {
        "pain_points": "Missing urgent emergency dispatch requests; leaking high-ticket sewer/line replacement calls to third-party directories; no immediate booking line.",
        "buzzwords": "trenchless main line repair, hydro-jetting diagnostics, 24/7 emergency dispatch routing, commercial service agreements",
        "hook_angle": "Focus on turning their high review authority on Google Maps into instant, real-time paid service bookings with zero phone friction."
    },
    "law firm": {
        "pain_points": "High friction client intake forms losing sensitive case leads; poor brand positioning failing to demonstrate immediate practice-area authority to premium retainers.",
        "buzzwords": "confidential case evaluation, specialized practice-area authority, multi-channel intake workflows, high-value retainer conversion",
        "hook_angle": "Build an elite, secure, low-friction intake funnel that establishes immediate authority and captures qualified litigation clients seamlessly."
    },
    "accounting firm": {
        "pain_points": "Failing to capture high-margin corporate advisory accounts; massive administrative overhead during peak tax seasons due to unoptimized manual file intake workflows.",
        "buzzwords": "fractional CFO advisory, secure client data portals, tax strategy optimization, automated monthly bookkeeping pipelines",
        "hook_angle": "Maximize their recurring business pipeline by introducing secure file-drop client portals that position them as premium corporate advisory partners."
    },
    "real estate agency": {
        "pain_points": "Losing premium listings to massive corporate platforms (Zillow/Property24); unoptimized property landing pages causing listing presentation bounces.",
        "buzzwords": "exclusive listing showcases, high-converting virtual tour modules, hyper-local market valuation funnels, buyer matching matrix",
        "hook_angle": "Build specialized single-property digital spaces and local market evaluation funnels to secure high-value exclusive listing contracts from sellers."
    },
    "medical clinic": {
        "pain_points": "High friction booking architectures leading to receptionist overhead and dropped digital appointments; missing secure patient intake pipelines.",
        "buzzwords": "streamlined patient intake protocols, multi-tier appointment scheduling, clinical workflow optimization, patient retention funnels",
        "hook_angle": "Position a specialized, secure booking suite to offload administrative staff overhead and capture higher patient booking conversions."
    },
    "dental clinic": {
        "pain_points": "High client acquisition costs for premium elective cases (implants, cosmetic crown/veneer cases); empty mid-week hygiene appointment tracking slots.",
        "buzzwords": "smile restoration cosmetic consulting, high-margin implant pipelines, automated recall scheduling, family practice block-booking",
        "hook_angle": "Target high-margin cosmetic and restorative case funnels while implementing rapid mid-week scheduling triggers to eliminate dead time."
    },
    "boutique hotel": {
        "pain_points": "Bleeding 15-25% top-line revenues to middleman booking channels (Agoda, Booking.com) due to a clunky internal checkout process; failing to show off property premium vibes visually.",
        "buzzwords": "direct-booking engine optimization, maximum room yield strategies, localized package experiences, premium property asset showcases",
        "hook_angle": "Expose how much revenue they are handing over to major OTAs and bypass them using an immersive direct-booking layout that maximizes net room yields."
    },
    "fitness center": {
        "pain_points": "High member churn; failing to sell high-margin corporate wellness plans or premium personal training bundles directly through the web storefront.",
        "buzzwords": "recurring membership conversion, high-tier personal training pipelines, corporate wellness packages, member retention workflows",
        "hook_angle": "Design a high-impact membership sign-up pipeline that automatically upsells personal training programs and onboarding packages during registration."
    },
    "default": {
        "pain_points": "Missing localized digital market share; high customer conversion friction due to zero centralized information/authority hub.",
        "buzzwords": "localized search footprint, consumer trust alignment, streamlined digital pipeline, automated conversion workflows",
        "hook_angle": "Leverage their excellent regional reputation and review counts into a high-converting digital storefront."
    }
}

def get_niche_parameters(category: str) -> dict:
    """Matches incoming lead categories to strict professional copy guidelines."""
    normalized = str(category).lower().strip()
    
    # Direct match or substring keyword lookup loops
    for key, strategies in NICHE_STRATEGY_MATRIX.items():
        if key in normalized or normalized in key:
            return strategies
            
    return NICHE_STRATEGY_MATRIX["default"]


# ---------------------------------------------------------------------------
# SERVICE PITCH MATRIX — for non-web-design service types discovered via
# job boards. Angle: Philippine-based agency pitching outsourcing to
# international companies that are actively hiring these roles in-house.
# ---------------------------------------------------------------------------
SERVICE_PITCH_MATRIX = {
    "video_editing": {
        "pain_points": (
            "Slow content output velocity due to in-house hiring bottlenecks; "
            "high per-video production costs eating into marketing budget; "
            "inability to scale content volume without scaling headcount."
        ),
        "buzzwords": (
            "asynchronous editing pipeline, rapid 48-hour turnaround SLA, "
            "batch content delivery, dedicated offshore editor, broadcast-quality output"
        ),
        "hook_angle": (
            "You're actively hiring a video editor — which means production demand is real "
            "but you're absorbing full-time overhead for what could be a dedicated offshore pipeline. "
            "Our Philippine-based video editing team delivers broadcast-quality output at "
            "60-70% below your current in-house or Western freelance rate."
        ),
        "cta": "Can I send over a sample edit of your existing content this week — zero cost, no strings?",
    },
    "bpo_customer_service": {
        "pain_points": (
            "High per-agent hiring and onboarding costs in home market; "
            "high attrition rates straining quality consistency; "
            "inability to offer 24/7 or multi-timezone coverage without blowing payroll."
        ),
        "buzzwords": (
            "dedicated offshore support team, 24/7 multi-timezone coverage, "
            "attrition-proof staffing, quality-controlled SLA, zero recruitment overhead"
        ),
        "hook_angle": (
            "You're hiring customer service staff in-house, which means you're absorbing "
            "full recruitment, onboarding, and attrition costs domestically. "
            "Our fully managed BPO team in the Philippines delivers the same quality coverage "
            "at a fraction of the cost — with a dedicated QA layer built in."
        ),
        "cta": "Would a 2-week pilot with 2 fully trained agents at no-commitment pricing be worth a 10-minute call?",
    },
    "bpo_data_entry": {
        "pain_points": (
            "Repetitive high-volume data tasks draining your core team's productive capacity; "
            "expensive domestic hires for non-strategic back-office functions; "
            "inconsistent output quality from one-off freelancers."
        ),
        "buzzwords": (
            "high-accuracy data pipelines, offshore back-office processing, "
            "structured output QA, scalable processing bandwidth, dedicated data ops team"
        ),
        "hook_angle": (
            "Your recent job posting signals a real need for scalable data processing bandwidth. "
            "Our Philippine-based data operations team handles exactly this at guaranteed accuracy "
            "rates — at 65% below your current domestic staffing cost."
        ),
        "cta": "Want us to process a small sample batch of your data this week to demonstrate accuracy before any commitment?",
    },
    "social_media_management": {
        "pain_points": (
            "Inconsistent posting schedules bleeding follower engagement; "
            "high monthly retainer costs from Western social media agencies; "
            "lack of dedicated platform-native content creation bandwidth."
        ),
        "buzzwords": (
            "content calendar architecture, platform-native creative strategy, "
            "engagement rate optimization, community management SLA, dedicated account manager"
        ),
        "hook_angle": (
            "You're building out a social media team in-house, which is expensive and slow to scale. "
            "Our dedicated offshore social media managers handle full content calendar execution, "
            "creative design, and community management at a monthly retainer that undercuts "
            "any Western agency by 60%."
        ),
        "cta": "Can I share a sample 2-week content plan built around your brand this week?",
    },
    "virtual_assistant": {
        "pain_points": (
            "Founder and executive time drowning in administrative overhead; "
            "high cost of a full-time domestic PA; "
            "difficulty finding reliable long-term virtual support that learns your workflow."
        ),
        "buzzwords": (
            "executive support operations, inbox zero management, "
            "calendar and schedule orchestration, research and reporting workflows, "
            "dedicated long-term VA placement"
        ),
        "hook_angle": (
            "You're hiring a remote executive or virtual assistant — meaning the demand is real "
            "and ongoing. We specialize in placing highly trained, long-term VAs from the Philippines "
            "for international executives and founders at a monthly cost that is a fraction of "
            "a domestic hire, with zero recruitment friction."
        ),
        "cta": "Would a complimentary 3-day trial with a hand-matched VA be a fair way to test the fit?",
    },
    "web_design": {
        "pain_points": (
            "Missing high-intent international search traffic due to zero or outdated web presence; "
            "losing qualified leads to competitors with professional digital storefronts; "
            "high Western agency quotes for straightforward web builds."
        ),
        "buzzwords": (
            "high-converting landing architecture, international SEO foundation, "
            "mobile-first design system, automated lead generation pipeline, "
            "performance-optimized build"
        ),
        "hook_angle": (
            "Your business deserves a digital presence that matches the quality of your service. "
            "We build high-converting, internationally positioned websites from our team in the "
            "Philippines — at a fraction of US or Australian agency rates."
        ),
        "cta": "Can I send over a free interactive mockup of what your homepage could look like this week?",
    },
}

# All service types that are outsourcing/BPO oriented (not web design)
OUTSOURCING_SERVICE_TYPES = {
    "video_editing",
    "bpo_customer_service",
    "bpo_data_entry",
    "social_media_management",
    "virtual_assistant",
}


def get_service_parameters(service_type: str) -> dict:
    """Returns pitch parameters for a given service type (video editing, BPO, VA, etc.)."""
    normalized = str(service_type).lower().strip()
    return SERVICE_PITCH_MATRIX.get(normalized, SERVICE_PITCH_MATRIX["web_design"])