# Open Data Product family map

The Open Data Product (ODP) standards are five Linux Foundation specifications for defining, cataloguing, connecting and delivering data products, plus a Python SDK that works with all of them. This page says what each one is for and how they depend on each other. It is the source that myPub's curated spec-map edges cite (`scripts/seed_odp_family.py`, `data/odp/odp_family.yaml`).

Versions as of 2026-11-16: ODPS 4.2, ODPC 1.0, ODPG 1.0, ODPV 1.1, ODPR 1.0 (draft), SDK `open-data-products` 0.7.0.

## How the specifications fit together

ODPS is the base: it describes one data product. ODPC adds the portfolio around products, ODPG adds the relationships between products and the value they create, ODPV supplies the shared terms all of them use, and ODPR describes repeatable work over the other four. Each specification can be used on its own; used together they form a machine-readable operating model for data product management.

```
             ODPR  recipes: how the work is done
            /    \
          ODPC -- ODPG     portfolio (catalogs) and relationships (graphs)
            \    /
             ODPS          one data product
   ODCS  <-- contracts      ODPV  shared vocabulary, used by all
   SDK   validates, generates and links all five
```

| Question | Specification |
| --- | --- |
| What is this one data product, and how is it accessed, priced, licensed and governed? | ODPS |
| Which products do we have, for which use cases and objectives? | ODPC |
| How do products, use cases, objectives, KPIs and policies connect, and what depends on what? | ODPG |
| What do the words mean, consistently across files and tools? | ODPV |
| How is the work of building, validating and releasing products done repeatably, by people or AI agents? | ODPR |

## ODPS: Open Data Product Specification

ODPS (version 4.2) is a vendor-neutral, machine-readable metadata model for one data product, written in YAML or JSON and validated by a JSON Schema. Its root `product` object holds ten components: `details` (name, productID, visibility, status, type), `productStrategy` (objectives and KPIs), `contract` (named data contract profiles, with a required `default`), `SLA` (declared dimensions plus SLA-as-code monitoring), `dataQuality` (declared targets plus executable checks in tools such as SodaCL, DQOps or Monte Carlo), `pricingPlans`, `license`, `dataAccess`, `dataHolder` and `paymentGateways`. Specification extensions use `x-` fields.

ODPS is the foundation the rest of the family builds on. Its data contract component aligns with the Open Data Contract Standard (ODCS), and its terms come from ODPV.

## ODPC: Open Data Product Catalogs

ODPC (version 1.0) is the portfolio layer around ODPS products. A `Catalog` holds `ProductReference` objects, `UseCase` objects (the demand side: why data is needed and by whom, with `dataNeeds`), `BusinessObjective` objects (strategic outcomes, with KPIs nested inside), and `Signal` objects (demand, competitive or risk indicators that may suggest new use cases or products).

A ProductReference does not copy a product's metadata. Its `productModel` block names the standard (ODPS, DPDS or internal), version, format and a `$ref` to the authoritative file, so ODPC requires ODPS but is not ODPS-only. ODPC deliberately leaves relationship fields out of its objects: relationships belong to ODPG. ODPC uses ODPV for terms, identifiers and relationship names.

## ODPG: Open Data Product Graphs

ODPG (version 1.0) is the relationship and intelligence layer. A graph document (`kind: Graph`) holds `nodes` and `edges`. Each node has an `id`, a `type` (DataProduct, UseCase, BusinessObjective, KPI, Domain, Dataset, API, Policy, Workflow, Agent, Capability, StrategicOpportunity) and a `$ref` to the resource it stands for: DataProduct nodes point at ODPS files, and nodes may point at ODPC catalog entries. Each edge has `from`, `to`, a `type` (uses, supports, contributesTo, measures, tracks, dependsOn, governedBy, exposes, identifies and others) and a `confidence` of high, medium or low.

ODPG requires ODPS and ODPC: it connects their objects. ODPC is for discovery and ODPG is for reasoning: value mapping, impact and dependency analysis, governance checks, and focused context for AI agents. Edge types should use ODPV relationship terms.

## ODPV: Open Data Product Vocabulary

ODPV (version 1.1) is the controlled vocabulary shared by the whole family: 78 terms in four groups (core 18, value 17, governance 19, relationships 24). Each term has a stable `id` and `uri`, a preferred label, a definition, `alsoKnownAs` aliases, related terms, and `usedIn` (the specifications that use it); relationship terms also give a domain and range. ODPV is deliberately a practical vocabulary rather than a heavy ontology. It is published as YAML (canonical), JSON, JSON-LD and SKOS Turtle, with interoperability profiles that map its terms to DCAT-3, ODCS-3, ODRL-2, PROV-O and OpenLineage.

ODPS, ODPC, ODPG and ODPR all cite ODPV. The term tables below are generated from ODPV 1.1.

## ODPR: Open Data Product Recipes

ODPR (version 1.0, draft) standardizes how data product work is declared, discovered, configured, validated, reviewed and handed off. A `Recipe` is a reusable workflow unit with intent, instructions, scope, a runtime reference, ordered steps, expected outputs, gates and whether human review is required. It supports four flow types: delivery flows (portfolio building, validation, localization, publishing, release review), product handoff flows (a `DataProductRecipe` manifest that developers and AI agents use to implement one product), agent discovery flows (bounded, grounded investigations such as finding affected use cases), and trigger-based flows (when a graph change makes work applicable). A `RuntimeProfile` references approved model configuration without embedding credentials, and a `RecipeCatalog` groups recipes.

ODPR requires the other specifications: a DataProductRecipe takes its `source-product-spec` from ODPS, its `portfolio-context` from ODPC, its `relationship-context` from ODPG, its terms from ODPV, and its `contract-plan` from ODCS 3.1.

## The SDK: open-data-products

The Open Data Products SDK (PyPI `open-data-products`, repository `odp-agent-sdk`, version 0.7.0) implements the whole family. Its CLI and Python API validate, explain and summarize ODPS, ODPC, ODPG, ODPV and ODPR documents; generate ODPS product drafts, ODPC fragments and ODPG graphs from source notes with local (Ollama, llama.cpp) or hosted LLMs; build and search catalogs (`odpc-build`, `odpc-search`); build, traverse and analyze graphs (`odpg-build`, `odpg-traverse`, `odpg-agent-context`); resolve and explain vocabulary terms (`odpv-resolve`, `odpv-explain`); run portfolio workflows; list, dry-run and execute ODPR recipes; and wrap ODCS contracts into ODPS drafts. `open-data-products serve` runs a stdio MCP server with safe tools for agents, and `open-data-products manifest --json` writes an ARWS-compatible agent manifest.

## ODCS: Open Data Contract Standard

The Open Data Contract Standard (ODCS, Bitol project, Linux Foundation) defines a data contract between producers and consumers: fundamentals, schema, data quality rules, service-level agreement, team and servers. It is not part of the ODP family, but ODPS data contracts align with it, ODPV maps to it in its ODCS-3 profile, ODPR contract plans are based on ODCS 3.1, and the SDK can wrap ODCS 3.0 to 3.2 contracts into ODPS 4.2 drafts.

## Building a data product with the family

1. Frame the demand in ODPC: the use case, the business objective it serves, and the KPIs that will show progress.
2. Describe the product in ODPS: details first (only a few fields are mandatory), then the data contract (or a reference to an ODCS contract), SLA, data quality, data access, license and, if it is sold, pricing plans.
3. Validate the product file with `open-data-products validate` and fix what the schema reports.
4. Add the product to the catalog as an ODPC ProductReference whose `productModel.$ref` points at the ODPS file.
5. Connect it in ODPG: the product supports the use case, the use case contributes to the objective, the KPI measures the objective, and any policies govern the product.
6. Check every term against ODPV with `open-data-products odpv-resolve`, so names mean the same thing across files.
7. Write an ODPR DataProductRecipe as the handoff for whoever builds the pipeline, citing the ODPS spec, the ODPG relationship context and the contract plan.
8. Re-validate the whole set in CI on every change, and let the graph show which use cases a change affects.

<!-- odpv:begin -->

## ODPV core terms

| Term | Definition | Also known as | Used in |
| --- | --- | --- | --- |
| Data Product (`DataProduct`) | A managed data offering designed for reuse, with defined ownership, access, quality, usage terms, and value context. | data product, data offering, reusable data asset, data product asset | ODPS, ODPC, ODPG |
| Product Details (`ProductDetails`) | The descriptive metadata block for a data product, including name, product identifier, description, value proposition, visibility, status, and version. | details, product details, product metadata, descriptive metadata | ODPS |
| Data Product Catalog (`DataProductCatalog`) | A managed collection of data products and related portfolio objects, such as use cases, objectives, KPIs, signals, and references. | data catalog, Catalog, ODPC Catalog, product catalog, data product portfolio, portfolio catalog | ODPC, ODPG |
| Data Product Graph (`DataProductGraph`) | A graph representation that connects data products, catalogs, use cases, objectives, KPIs, signals, and governance objects through defined relationships. | data product graph, portfolio graph, knowledge graph, product relationship graph | ODPG |
| Dataset (`Dataset`) | A structured collection of data that may be part of, or exposed through, a data product. Aligns with DCAT where applicable. | data set, data collection, source dataset, data resource | ODPS, ODPC |
| Distribution (`Distribution`) | A specific accessible form of a dataset, such as a file, API response, export, or downloadable representation. Aligns with DCAT where applicable. | data file, data download, export, data representation | ODPS |
| Data Service (`DataService`) | A service that provides access to data or data operations, such as an API, query endpoint, or data delivery service. Aligns with DCAT where applicable. | data API, API, data endpoint, query service, delivery service | ODPS, ODPG |
| Agent (`Agent`) | An AI agent, automation actor, or software system that can use data products, services, workflows, or graph context to perform a task. | AI agent, automation actor, autonomous agent, software agent | ODPG |
| Workflow (`Workflow`) | A business, operational, analytical, or technical process that coordinates activities, systems, agents, data products, or services. | process, business workflow, technical workflow, orchestration flow | ODPG |
| Capability (`Capability`) | A reusable business, organizational, analytical, operational, or technical ability that can be supported by data products, workflows, agents, or services. | business capability, technical capability, organizational capability, reusable capability | ODPG |
| Provider (`Provider`) | A person, team, organization, or system that provides or publishes a data product, dataset, distribution, or data service. | publisher, supplier, source provider, data provider | ODPS, ODPC, ODPG |
| Data Holder (`DataHolder`) | A legal entity or accountable organization that holds, controls, or is responsible for a data product or the data it contains. | dataHolder, data holder, holding organization, accountable legal entity | ODPS |
| Consumer (`Consumer`) | A person, team, organization, system, or agent that uses or consumes a data product, dataset, distribution, or data service. | user, data user, customer, recipient, consuming agent | ODPS, ODPC, ODPG |
| Owner (`Owner`) | A person, team, or organization accountable for a data product, catalog, dataset, service, or related object. | accountable owner, product owner, business owner, asset owner | ODPS, ODPC |
| Steward (`Steward`) | A person, team, or organization responsible for the operational management, quality, metadata, and lifecycle of a data product or related object. | data steward, product steward, metadata steward, quality steward | ODPS, ODPC |
| Domain (`Domain`) | A business, organizational, subject, or functional area used to group data products and related objects. | business domain, data domain, subject area, functional area | ODPS, ODPC, ODPG |
| Identifier (`Identifier`) | A stable value used to uniquely identify a vocabulary term, specification object, product, catalog item, or graph node. | id, unique id, persistent id, node id | ODPS, ODPC, ODPG |
| Reference (`Reference`) | A link or pointer to another object, specification file, external vocabulary term, system record, or graph node. | link, pointer, external reference, object reference, GraphReference, ProductReference, ProductModel, graph reference, product reference, product model reference | ODPS, ODPC, ODPG |

## ODPV value terms

| Term | Definition | Also known as | Used in |
| --- | --- | --- | --- |
| Use Case (`UseCase`) | A practical business, operational, analytical, or technical scenario where one or more data products are used to create value. | use case, scenario, business scenario, application case | ODPC, ODPG |
| Business Objective (`BusinessObjective`) | A business goal or intended outcome that data products, use cases, or portfolio actions support. | objective, business goal, strategic goal, target outcome | ODPC, ODPG |
| KPI (`KPI`) | A measurable indicator used to track progress toward a business objective, outcome, or portfolio goal. | key performance indicator, metric, performance measure, success measure | ODPC, ODPG |
| Impact (`Impact`) | The expected or observed effect created by a data product, use case, signal, or portfolio decision. | effect, value impact, business impact, observed effect | ODPC, ODPG |
| Signal (`Signal`) | An observed demand, trend, risk, gap, usage pattern, policy change, market change, or operational event that may require action. | market signal, demand signal, trigger, observation | ODPC, ODPG |
| Gap (`Gap`) | A missing data product, dataset, capability, quality level, access method, relationship, or portfolio coverage area. | missing capability, coverage gap, data gap, portfolio gap | ODPC, ODPG |
| Priority (`Priority`) | A ranking or importance level used to guide portfolio planning, product development, investment, or remediation. | importance level, ranking, urgency, priority level | ODPC, ODPG |
| Outcome (`Outcome`) | A desired or achieved result connected to a business objective, use case, KPI, or impact statement. | result, target result, achieved result, intended outcome | ODPC, ODPG |
| Data Need (`DataNeed`) | A required data input, data product, dataset, attribute, capability, or access pattern needed to support a use case or objective. | data requirement, data demand, required data, information need | ODPC, ODPG |
| Benefit (`Benefit`) | A positive business, operational, financial, social, or technical value expected from a data product, use case, or portfolio action. | value, expected benefit, positive impact, business benefit | ODPC |
| Demand (`Demand`) | Evidence of need or interest from consumers, stakeholders, systems, markets, policies, or operational processes. | need, interest, request, demand signal | ODPC, ODPG |
| Opportunity (`Opportunity`) | A possible area for value creation, reuse, innovation, service improvement, monetization, or better decision-making. | value opportunity, improvement area, innovation opportunity, reuse opportunity | ODPC, ODPG |
| Strategic Opportunity (`StrategicOpportunity`) | An inferred or declared opportunity with strategic relevance, such as a portfolio improvement, optimization, unmet need, or emerging value area. | strategic opportunity, portfolio opportunity, strategic value opportunity, emerging opportunity | ODPG |
| Product Strategy (`ProductStrategy`) | The strategic plan or context for a data product, including objectives, alignment, dates, status, and KPIs used to describe intended value. | productStrategy, product strategy, product strategic plan, strategic product context | ODPS |
| Risk (`Risk`) | A possible negative event, weakness, exposure, or uncertainty that may affect data product value, trust, access, compliance, or operation. | issue, exposure, threat, concern | ODPC, ODPG |
| Value Proposition (`ValueProposition`) | A short explanation of why a data product, use case, or portfolio item matters and what value it is expected to create. | value statement, value case, product value, business value statement | ODPS, ODPC |
| Portfolio Priority (`PortfolioPriority`) | A portfolio-level priority assigned to a data product, use case, signal, gap, or objective. | portfolio ranking, portfolio importance, investment priority, planning priority | ODPC, ODPG |

## ODPV governance terms

| Term | Definition | Also known as | Used in |
| --- | --- | --- | --- |
| Data Quality (`DataQuality`) | The expected, measured, or reported quality of a data product, dataset, distribution, or data service. | quality, data quality score, quality assessment, quality metrics | ODPS, ODPC, ODPG |
| SLA (`SLA`) | A service-level agreement or expectation that defines operational commitments for a data product, distribution, or data service. | service level agreement, service commitment, operational commitment, service expectation | ODPS, ODPG |
| License (`License`) | The legal terms that define how a data product, dataset, distribution, or data service may be used, shared, or redistributed. | license, data license, usage license, legal license, redistribution terms | ODPS, ODPC, ODPG |
| Data Access (`DataAccess`) | The access configuration or access description for a data product, dataset, distribution, or data service. | dataAccess, data access, access configuration, access description | ODPS, ODPC, ODPG |
| Access Method (`AccessMethod`) | The method used to access a data product, dataset, distribution, or data service, such as API, file download, query endpoint, or platform access. | access channel, delivery method, access interface, data access method | ODPS, ODPC, ODPG |
| Pricing Plan (`PricingPlan`) | A pricing, cost, subscription, or commercial plan that describes how a data product or data service may be paid for, charged, or monetized. | pricingPlans, pricing plan, commercial plan, subscription plan | ODPS |
| Payment Gateway (`PaymentGateway`) | A payment integration, payment service, or gateway used to process commercial access to a data product or data service. | paymentGateways, payment gateway, payment integration, payment service | ODPS |
| Support (`Support`) | The support model or contact channel for help, issue handling, or operational assistance related to a data product or data service. | support, product support, support contact, help channel | ODPS |
| Agreement (`Agreement`) | A formal or informal agreement that defines usage, commercial, legal, operational, or governance terms for a data product or data exchange. | data agreement, usage agreement, service agreement, commercial agreement | ODPS, ODPC, ODPG |
| Policy (`Policy`) | A rule, guideline, or governance statement that applies to a data product, catalog, graph, use case, access method, or related object. | governance policy, rule, guideline, control policy | ODPS, ODPC, ODPG |
| Compliance Rule (`ComplianceRule`) | A specific rule or requirement used to assess, enforce, or document compliance. | compliance requirement, control rule, regulatory rule, validation rule | ODPS, ODPC, ODPG |
| Sensitivity (`Sensitivity`) | A classification that indicates how sensitive a data product, dataset, attribute, distribution, or service is from a privacy, security, commercial, or operational perspective. | sensitivity level, data sensitivity, security classification, privacy classification | ODPS, ODPC |
| Usage Rights (`UsageRights`) | The rights granted to consumers for using, sharing, modifying, deriving, or redistributing a data product or dataset. | use rights, permitted use, usage permissions, allowed use | ODPS, ODPC |
| Retention (`Retention`) | The rules or expectations for how long data, metadata, logs, agreements, or related records should be retained. | retention rule, retention period, record retention, data retention | ODPS, ODPC |
| Access Condition (`AccessCondition`) | A condition that must be met before a consumer can access a data product, dataset, distribution, or data service. | access rule, access requirement, eligibility condition, access constraint | ODPS, ODPC, ODPG |
| Governance Profile (`GovernanceProfile`) | A reusable governance classification that describes the expected level of control, assurance, review, or operational maturity for a data product or catalog item. | governance level, assurance profile, control profile, maturity profile | ODPS, ODPC |
| Data Agreement (`DataAgreement`) | A structured agreement that defines the allowed use, responsibilities, obligations, and constraints for a data product or data exchange. | data sharing agreement, data usage agreement, data exchange agreement, product agreement | ODPS, ODPC |
| Data Contract (`DataContract`) | A technical or operational contract that defines expectations for schema, quality, delivery, compatibility, and change management. | schema contract, delivery contract, operational contract, technical contract | ODPS |
| Stewardship (`Stewardship`) | The assigned responsibility model for managing data product metadata, quality, lifecycle, and operational health. | data stewardship, product stewardship, metadata stewardship, lifecycle responsibility | ODPS, ODPC |

## ODPV relationship terms

Relationship terms name the edges in ODPG graphs, with the object types they usually connect.

| Relationship | Definition | From | To |
| --- | --- | --- | --- |
| `supports` | Indicates that one object helps enable, serve, or make another object possible, such as a data product supporting a use case. | DataProduct, DataProductCatalog, UseCase | UseCase, BusinessObjective, Outcome |
| `requires` | Indicates that one object needs another object to be complete, useful, or executable, such as a use case requiring a data product. | UseCase, DataProduct, BusinessObjective | DataProduct, Dataset, DataNeed |
| `uses` | Indicates that one object uses another object as part of execution, operation, analysis, automation, or decision-making. | UseCase, Agent, Workflow, DataProduct | DataProduct, DataService, Dataset, Workflow |
| `contributesTo` | Indicates that one object helps advance another object, such as a use case contributing to a business objective. | DataProduct, UseCase, KPI | BusinessObjective, Outcome, Impact |
| `measures` | Indicates that one object measures another object, such as a KPI measuring a business objective or outcome. | KPI, Signal | BusinessObjective, Outcome, Impact |
| `belongsTo` | Indicates that one object is assigned to, grouped under, or included in another object, such as a data product belonging to a catalog. | DataProduct, UseCase, BusinessObjective | DataProductCatalog, Domain |
| `dependsOn` | Indicates that one object has a dependency on another object, such as a data product depending on another product, service, or dataset. | DataProduct, DataService, UseCase | DataProduct, Dataset, DataService |
| `governedBy` | Indicates that one object is governed by another object, such as a data product governed by a license, policy, agreement, or compliance rule. | DataProduct, Dataset, AccessMethod | Policy, License, Agreement, ComplianceRule |
| `providedBy` | Indicates that one object is provided, published, or made available by a provider. | DataProduct, Dataset, DataService | Provider, Owner |
| `consumedBy` | Indicates that one object is used, accessed, or consumed by a consumer. | DataProduct, Dataset, DataService | Consumer |
| `produces` | Indicates that one object creates, generates, outputs, or makes available another object, such as a workflow producing a dataset or data service. | Workflow, DataProduct, Agent, DataService | Dataset, DataService, DataProduct, Distribution |
| `consumes` | Indicates that one object consumes data, services, outputs, or interfaces from another object. | DataProduct, Agent, Workflow, DataService | Dataset, DataService, DataProduct, Distribution |
| `indicates` | Indicates that one object points to, reveals, or suggests another object, such as a signal indicating a gap, demand, risk, or opportunity. | Signal, KPI | Gap, Demand, Risk, Opportunity |
| `identifies` | Indicates that one object identifies, detects, discovers, or names another object, such as an agent identifying a strategic opportunity. | UseCase, Agent, KPI, Signal | StrategicOpportunity, Opportunity, Gap, Risk |
| `relatedTo` | Indicates a general relationship between two objects when a more specific relationship type is not available. | DataProduct, UseCase, BusinessObjective, GovernanceProfile | DataProduct, UseCase, BusinessObjective, GovernanceProfile |
| `ownedBy` | Indicates that one object is owned or accountable to a person, team, organization, domain, or capability. | DataProduct, DataService, Dataset, Workflow, Agent | Owner, Domain, Capability, Provider |
| `alignsWith` | Indicates that one object has strategic, semantic, organizational, or capability alignment with another object. | StrategicOpportunity, Capability, UseCase, DataProduct | BusinessObjective, Domain, Capability, StrategicOpportunity |
| `partOf` | Indicates that one object is part of a larger object, such as a catalog item being part of a catalog. | DataProduct, Dataset, Distribution | DataProductCatalog, DataProduct, Dataset |
| `hasPart` | Indicates that one object contains or includes another object. | DataProductCatalog, DataProduct, Dataset | DataProduct, Dataset, Distribution |
| `derivedFrom` | Indicates that one object is derived from another object, such as an insight, product, dataset, or signal derived from a source. | DataProduct, Dataset, Signal | Dataset, DataProduct, Reference |
| `impacts` | Indicates that one object affects, changes, influences, or has an impact on another object. | Policy, DataProduct, Workflow, StrategicOpportunity | UseCase, KPI, BusinessObjective, Outcome |
| `exposes` | Indicates that one object exposes a data service, interface, endpoint, distribution, or access method. | DataProduct, Workflow, Dataset, Agent | DataService, AccessMethod, Distribution |
| `replaces` | Indicates that one object replaces another object, such as a newer data product replacing an older version. | DataProduct, Dataset, DataService | DataProduct, Dataset, DataService |
| `versionOf` | Indicates that one object is a version of another object. | DataProduct, Dataset, DataService | DataProduct, Dataset, DataService |

<!-- odpv:end -->
