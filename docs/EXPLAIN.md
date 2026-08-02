# PulseOps, explained without the jargon

The main README assumes you already know what a data warehouse is. This one doesn't.

If any term in the main README made you stop and squint, it's defined here. Read it top to bottom and the rest of the project should make sense.

---

## The one-sentence version

Fake restaurant orders flow through a queue into a database, and along the way every single one is checked against a written set of rules. The ones that pass get stored. The ones that fail get stored **separately**, along with the reason they failed.

That second half is the whole project.

Plenty of people can move data from A to B. What makes this worth looking at is that it **knows when the data is wrong, and can prove how often it catches it**.

## The problem it exists to solve

Imagine you run five restaurants. Monday morning the dashboard says one outlet's revenue fell 40% overnight.

Two completely different things could have happened.

**One: sales genuinely dropped.** Something real happened and you need to go find out what.

**Two: the till software was updated on Sunday.** It now sends the order total under a different name, and your pipeline has been quietly recording those orders as zero ever since. Sales are fine. Your *data* is broken.

Most pipelines cannot tell you which. They throw away the evidence: bad records get either silently dropped or silently loaded as blanks. PulseOps keeps every rejected record and the reason it was rejected, so the answer is a query rather than an argument.

## Follow one order through the whole thing

The clearest way to understand this is to trace a single order from invention to storage.

### 1. Invent the order

*`generator/generate.py`, runs on your laptop*

There's no real restaurant, so we manufacture believable orders. Not random noise: lunch and dinner are busier, the flagship outlet gets more orders than the small one, and e-wallet payments fail slightly more often than cards.

```
2 x Nasi Lemak Ayam Rendang   RM 29.80
1 x Teh Tarik                 RM  4.50
                       total  RM 34.30
outlet OUT-KL-001 - card - captured
```

Then we **deliberately break some of them**. About 5%. And we write down exactly which ones we broke and how, in a file called the **manifest**.

That file is why the project can say "my checks caught 188 out of 188" instead of "my checks seem to work". It's an answer key.

### 2. Send it to the queue

*`ingest/publish.py` into Google Pub/Sub, runs in the cloud*

A **queue** is a waiting room for messages. The thing producing orders and the thing storing them don't have to be awake at the same time, or run at the same speed. The order sits in the queue until something collects it.

We send **everything**, including the orders we deliberately broke. That feels wrong and it's the most important decision in the project. More on it below.

### 3. Collect it from the queue

*`ingest/subscribe.py`*

A **subscriber** pulls messages off the queue in batches. It only tells the queue "done, you can forget this one" *after* the record is safely stored. If your laptop dies halfway, the queue re-sends everything it never got confirmation for. Nothing is lost.

### 4. Check it against the contract

*`contracts.py`, the rulebook*

The **contract** is a written list of what a valid order looks like. Does it name an outlet? Is the price a positive number? Is the timestamp in a format we can read? Does quantity x unit price actually equal the line total?

It collects **every** problem with a record, not just the first one it hits, so a record broken four ways gets all four reasons recorded:

```json
{
  "code":   "missing_field",
  "path":   "outlet_id",
  "detail": "required field outlet_id absent"
}
```

### 5. Send it through one of two doors

*BigQuery, two tables in the cloud*

This is the moment everything else was building toward. Pass, and it goes to `raw`. Fail, and it goes to `quarantine` with its reasons attached and its original contents kept intact.

Nothing is ever deleted. A quarantined record keeps its payload, which means once whoever broke it upstream fixes their side, those records can be replayed and the numbers repair themselves.

### 6. Reshape it for humans

*dbt, into a star schema*

Raw data is shaped for machines. A dashboard wants "revenue by outlet by day", which means reorganising it into a **star schema**: one big table of things that happened, surrounded by small tables describing the outlets, the menu items and the calendar.

### 7. Two other sources arrive daily

*Airflow*

Orders stream continuously. Two other things arrive once a day on a schedule: a **stock snapshot** (how many of each dish each outlet had) and the **weather** (which is real data, pulled from a free public API).

**Airflow** is the tool that runs jobs on a schedule and in the right order. It makes sure the weather has landed before anything tries to use it.

### 8. Ask it questions

*The copilot*

An AI assistant that can read the warehouse and answer questions about it, in words. Ask it "did sales drop or is the pipeline broken" and it checks both and tells you which, along with which tables it looked at.

It can **only read**. It cannot change or delete anything, and that's enforced in code rather than by asking it nicely.

---

## Five decisions worth being able to defend

These are the ones an interviewer will poke at.

### Why send broken data on purpose?

Because a producer you control is not a producer. Real tills, running someone else's software, send whatever they want.

If the publisher quietly filtered out bad records, you'd have built a pipeline that can never *receive* bad data. The quarantine table would stay empty forever and the entire thing you set out to demonstrate would vanish. So the checking happens at the far end, where the reason for each rejection can be recorded.

### Why is the raw table never edited?

Because a pipeline that rewrites its own history cannot be audited. When you find a bug in how something was calculated, the fix is to rebuild everything downstream from raw, not to quietly patch rows and hope. Raw is the record of what actually arrived.

### Why write down which records you broke?

Because otherwise "my quality checks work" is just a claim.

The manifest is the answer key. It turns the detection rate into a measurement anyone can reproduce, and the test suite fails the build if that number ever drops. This is the single biggest difference between this and a portfolio project that only looks impressive.

### Why admit some faults aren't caught at ingest?

Because they genuinely cannot be.

Spotting a **duplicate** needs the other records. Spotting a **menu item that doesn't exist** needs the menu. Spotting a **late arrival** needs to know when the reporting window closed. None of those are visible from one record in isolation.

So they're tagged as the warehouse layer's job rather than counted as a win at ingest. Claiming 100% by quietly redefining what counts is the kind of thing that falls apart under one interview question.

### Why refuse to repair some records?

Because repairing format is fine and inventing values is not.

A record whose total was *renamed* is recoverable, because the number is still sitting there under a different key. A record with **no outlet id** is not, because nothing on earth tells you which restaurant took that order.

Every repair in the project is a pure rename or reformat of data that is already present, and there are tests whose only job is to fail if that ever stops being true. A green dashboard built on invented numbers is worse than a red one.

---

## Jargon decoder

| Term | What it means |
|---|---|
| **Pub/Sub** | Google's message queue. A waiting room for data, so sender and receiver don't have to run at the same time or speed. |
| **BigQuery** | Google's data warehouse. A database built for analysing enormous tables, rather than for serving an app. |
| **Terraform** | Infrastructure as code. Instead of clicking around a console, you describe what cloud resources should exist in a file, and one command makes reality match it. |
| **dbt** | A tool for transforming data inside the warehouse using SQL, with version control and tests. |
| **Airflow** | A scheduler. Runs jobs daily, in dependency order, with retries when something fails. |
| **Data contract** | A written, versioned agreement about the exact shape of the data, enforced by code so breaking it is loud rather than silent. |
| **Quarantine** | A separate table for records that failed the contract, kept with their reasons so they can be diagnosed and re-sent. |
| **Raw / staging / mart** | The three layers. Raw is untouched arrivals. Staging is cleaned and deduplicated. Mart is reshaped for dashboards. Each layer has exactly one job. |
| **Partition & cluster** | Two ways of physically organising a huge table so queries read less of it. Partitioning splits by day, clustering sorts within that. Less data scanned means faster and cheaper. |
| **Idempotent** | Running it twice gives the same result as running it once. Vital when a queue might deliver the same message twice, which they all do. |
| **At-least-once delivery** | The queue guarantees it will deliver every message, but occasionally sends one twice. That's why deduplication is your problem, not the queue's. |
| **Dead letter topic** | Where a message goes after failing to process too many times, so one poisonous record can't block the queue forever. |
| **p95 latency** | 95% of requests were faster than this. Reported instead of an average because the average hides exactly the slow tail you care about. |
| **Star schema** | A warehouse layout: one central table of events (a *fact*), surrounded by descriptive lookup tables (*dimensions*) for outlets, menu items and dates. |
| **Surrogate key** | A made-up id used to join tables, instead of relying on a business id that might change. |
| **Unknown member** | A single placeholder row in a lookup table. Records pointing at something that doesn't exist attach to it, so they survive and stay countable instead of vanishing. |
| **Ground truth** | The answer key. Here it's the manifest recording exactly which records were broken, so detection rates are measured rather than asserted. |
| **LLM-as-agent / tool calling** | Giving an AI a fixed set of functions it may call. It decides which to use; it cannot do anything outside that list. |
| **Eval suite** | A set of test questions with known-correct answers, used to score an AI rather than eyeball it. |

---

## What the measurements keep catching

Worth knowing, because it's the part that turned out to matter most.

Every measurement layer in this project has caught a bug in the layer below it:

- The **manifest** caught SQL that measured lateness from the wrong clock, flagging every order in the warehouse as late.
- The **eval suite** caught a security guard that was silently blanking text inside queries, so `where status = 'failed'` ran as `where status = ''` and returned confidently wrong answers.
- The eval also found a **missing column** in the star schema, and caught the AI inventing a table name because nobody had given it the schema.
- Running the **CI job locally** caught a bug where the same random seed didn't actually produce the same data, which broke the project's central claim for four days without anyone noticing.
- Looking at the **dashboard** caught a fault injector that changed a timestamp's value as well as its format, inflating one fault count by 24.

None of those were caught by reading the code. Every one was caught by something checking a number against something else that knew better.

---

Back to the [main README](../README.md).
