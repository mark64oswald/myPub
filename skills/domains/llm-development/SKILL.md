# LLM Development Skill

## Overview

This skill guides Claude in LLM/AI development topics using the myPub knowledge base.

**Status:** Placeholder - To be generated from indexed chapters

## Domain Coverage

LLM and AI development encompasses:

- Prompt engineering
- Fine-tuning and training
- RAG (Retrieval Augmented Generation)
- Agent architectures
- Embeddings and vector stores
- Evaluation and benchmarking
- Safety and alignment
- Deployment and scaling

## Key Concepts

*To be populated from concept graph:*

- [ ] Prompt engineering patterns
- [ ] RAG architecture
- [ ] Vector databases
- [ ] Embeddings
- [ ] Fine-tuning approaches
- [ ] Agent patterns
- [ ] Tool use / function calling
- [ ] Evaluation metrics

## Source Books

Key books in collection:

- Building LLM Apps (various)
- Prompt Engineering guides
- NLP with Transformers
- Deep Learning (Goodfellow)
- *[Additional from your collection]*

## Common Patterns

### RAG (Retrieval Augmented Generation)
- Document chunking strategies
- Embedding selection
- Retrieval optimization
- Context window management

### Agent Development
- ReAct pattern
- Tool selection
- Memory management
- Multi-agent coordination

### Evaluation
- Benchmark design
- Human evaluation
- Automated metrics
- A/B testing

## Generation Instructions

To populate this skill:

```bash
python scripts/generate_skill.py --topic "LLM development" --output skills/domains/llm-development/
```
