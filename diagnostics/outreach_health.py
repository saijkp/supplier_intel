"""Free DNS evidence and outreach heartbeat checks for doctor --live."""
import json
import os


def sending_domain_checks(resolve=None):
    if resolve is None:
        import dns.resolver
        resolve=lambda name: [str(r).strip('"').replace('" "','') for r in dns.resolver.resolve(name,'TXT',lifetime=5)]
    from outreach.always_on import mailbox_config
    results=[]
    selectors=json.loads(os.getenv('OUTREACH_DKIM_SELECTORS','{}'))
    for host in sorted({a.rpartition('@')[2] for a in mailbox_config()}):
        checks=[('SPF',host,'v=spf1'),('DMARC','_dmarc.'+host,'v=DMARC1')]
        for selector in selectors.get(host,[]):
            checks.append(('DKIM',selector+'._domainkey.'+host,'p='))
        if not selectors.get(host):
            results.append({'domain':host,'check':'DKIM','ok':False,'detail':'Configure OUTREACH_DKIM_SELECTORS from provider settings'})
        for label,name,marker in checks:
            try:
                records=resolve(name)
                ok=any(marker.casefold() in value.casefold() for value in records)
            except Exception:
                ok=False
            results.append({'domain':host,'check':label,'ok':ok,'detail':'DNS record present' if ok else 'DNS record missing or lookup failed'})
    return results
