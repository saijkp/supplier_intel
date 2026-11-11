"""Drafts based on the curated walk-list pitch; Sai must approve each list."""
def steps(segment):
    pitch = {
        'developer': 'Could we price joinery, kitchens, wardrobes, doors or FF&E against your project drawings?',
        'architect': 'We source joinery, kitchens, wardrobes, doors and loose furniture made to drawing through partner factories in China for UK projects. Could we price one item on a residential or hospitality project?',
        'interior_designer': 'Could we price one item to your drawings for a UK interiors or FF&E project, through our partner factories in China?',
        'showroom_dealer': 'When a client needs a bespoke item beyond your stocked range, could we price it to drawing through our partner factories in China for UK delivery?',
        'uk_brand': 'Could we discuss second-sourcing a component or product line through partner factories in China, with samples and factory checks agreed for the brief?',
        'distributor': 'Could we discuss an own-label run through partner factories in China, with quantities and applicable test reports agreed for the product?',
    }[segment]
    return [
        {'channel':'email','day_offset':0,'subject':'One item to drawing for {company}', 'template':'Hello {first_name}.\n'+pitch},
        {'channel':'linkedin_task','day_offset':0,'template':'Hello {first_name}. '+pitch},
        {'channel':'linkedin_task','day_offset':3,'template':'Hello {first_name}. Would it help to discuss one current sourcing brief for {company}?'},
        {'channel':'email','day_offset':5,'subject':'A drawing to price for {company}', 'template':'Hello {first_name}.\nWould you like us to look at one drawing or sourcing brief? We can discuss samples and the applicable documentation before an order.'},
        {'channel':'visit_task' if segment in ('architect','interior_designer','showroom_dealer') else 'call_task','day_offset':10,'template':'Ask who leads interiors, FF&E or sourcing at {company}. Record outcome. Check objections and TPS/CTPS before any call.'},
        {'channel':'email','day_offset':14,'subject':'Shall I leave this with you, {company}?','template':'Hello {first_name}.\nI will leave this with you. If an item to drawing comes up, you can reply with a brief and we can discuss it.'},
    ]
