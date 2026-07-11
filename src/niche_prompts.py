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