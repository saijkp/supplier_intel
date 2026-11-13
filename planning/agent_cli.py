"""Explicit local trial commands. Never scheduled implicitly."""
import json
import os
import click
from planning.service import PlanningService
from planning.portal_agents import AgentTrial, trial_report

@click.group('planning')
def planning_cli():
    pass

@planning_cli.command('agent-trial')
@click.option('--limit', type=click.IntRange(1, 500), default=20)
@click.option('--authorities', default='', help='Comma-separated council names.')
def agent_trial(limit, authorities):
    if not os.getenv('SUPPLIER_INTEL_DB_PATH'):
        raise click.ClickException('Set SUPPLIER_INTEL_DB_PATH to an isolated local trial database first.')
    click.echo(json.dumps(AgentTrial(PlanningService()).run(limit, tuple(a.strip() for a in authorities.split(',') if a.strip())), indent=2))

@planning_cli.command('agent-report')
def agent_report():
    click.echo(json.dumps(trial_report(PlanningService().repo), indent=2))


@planning_cli.command('firm-trial')
@click.option('--limit', type=click.IntRange(1, 500), default=3, help='Maximum new firms to attempt.')
@click.option('--authorities', default='', help='Comma-separated council names.')
@click.option('--max-reveals', type=click.IntRange(0, 100), default=10)
def firm_trial(limit, authorities, max_reveals):
    if not os.getenv('SUPPLIER_INTEL_DB_PATH'):
        raise click.ClickException('Set SUPPLIER_INTEL_DB_PATH to an isolated local trial database first.')
    from planning.firm_trial import FirmTrial
    click.echo(json.dumps(FirmTrial(PlanningService()).run(limit, tuple(a.strip() for a in authorities.split(',') if a.strip()), max_reveals), indent=2))
