"""Replay an explicit, timestamped paper order plan against the research tape."""
import json
from pathlib import Path
from .engine import Engine
from .paper import PaperDesk
from .semantics import timestamp
from .store import Store


def replay_orders(tape,plan_path,output_path):
    if Path(output_path).exists():raise ValueError('Choose a new paper database; replay never overwrites a run')
    plan=json.loads(Path(plan_path).read_text())
    if not isinstance(plan,list):raise ValueError('Order plan must be a JSON array')
    for order in plan:
        order['decision_ms']=timestamp(order.get('timestamp'))
        if order['decision_ms'] is None:raise ValueError('Each decision requires an explicit UTC timestamp')
    plan.sort(key=lambda o:o['decision_ms'])
    store=Store(tape);engine=Engine(store,persist=False);engine.restoring=True
    clock=[store.stats()['first_received_ms'] or 0]
    desk=PaperDesk(engine,output_path,clock=lambda:clock[0],auto_policies=False);index=0;rejected=[]
    try:
        for record in store.raw():
            arrival=max(clock[0],record['received_ms'])
            # Decisions precede same-millisecond packets, a conservative tie rule.
            while index<len(plan) and plan[index]['decision_ms']<=arrival:
                order=plan[index];index+=1
                if order['decision_ms']<clock[0]:
                    rejected.append(dict(order=order,error='Decision precedes available tape'));continue
                clock[0]=order['decision_ms'];desk.advance()
                try:desk.submit(order)
                except ValueError as error:rejected.append(dict(order=order,error=str(error)))
            clock[0]=arrival;engine.apply(record);desk.advance()
        report=dict(paper_database=str(output_path),tape=str(tape),code_revision=engine.code_hash,
            last_available_ms=clock[0],submitted=index-len(rejected),rejected_decisions=rejected,
            decisions_beyond_tape=plan[index:],scoreboard=desk.snapshot(),
            timing='receipt order; same-millisecond decisions precede packets; no quotes after the replay clock')
        Path(str(output_path)+'.report.json').write_text(json.dumps(report,indent=2)+'\n')
        return report
    finally:desk.close();store.close()
