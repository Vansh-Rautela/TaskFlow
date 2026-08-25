# Implementation Plan: TaskFlow AI Support Agent

## Overview
This document outlines the comprehensive verification and implementation plan to ensure all required components work properly:
- OpenRouter API + Local LLM integration
- Draft Response generation
- Received Query processing
- Correct Intent Classification
- Telegram Notification system
- Vector DB with embedding models
- Accuracy for RAG (Retrieval-Augmented Generation)

## Current State Assessment

### Components Already Implemented
✅ **LLM Providers**: OpenRouter, Claude, Ollama (with failover)
✅ **Vector Store**: Qdrant with FastEmbed (dense + sparse embeddings)
✅ **Intent Classification**: Structured LLM classification with fallback
✅ **Draft Generation**: Context-grounded response generation with citations
✅ **Retrieval**: Hybrid dense+sparse RRF search with sufficiency checking
✅ **Alert System**: Email, Telegram, Slack, and custom webhooks
✅ **Pipeline**: Complete orchestrator with safety gates
✅ **Testing Framework**: Unit, integration, and pipeline tests

### Components Currently Missing/Incomplete
❌ **ML Training Pipeline**: Classifier training and evaluation (ml/ directory is empty)
❌ **Data Generation Scripts**: Full dataset generation pipeline
❌ **Demo Scenarios**: Complete end-to-end demo functionality
❌ **Full Integration Tests**: Complete system integration tests

## Detailed Implementation Plan

### Phase 1: Core Component Verification (Immediate)

#### 1.1 Verify OpenRouter API Integration
- Test OpenRouter provider with valid API key
- Verify structured output parsing
- Check failover mechanism to Claude/Ollama

#### 1.2 Verify Local LLM Integration  
- Test Ollama provider with qwen2.5 model
- Verify offline fallback capability
- Validate model loading and performance

#### 1.3 Validate Vector Database
- Test Qdrant initialization and collection creation
- Verify FastEmbed model loading (dense and sparse)
- Check embedding generation and storage
- Validate hybrid search functionality

#### 1.4 Confirm Intent Classification Accuracy
- Test classification service with sample queries
- Verify fallback mechanism for edge cases
- Check confidence scores accuracy

#### 1.5 Validate Draft Generation
- Test draft generation with retrieved context
- Verify citation inclusion
- Check structured output compliance

### Phase 2: Integration Testing

#### 2.1 End-to-End Pipeline Testing
- Run complete pipeline with sample messages
- Verify all stages execute correctly
- Test safety gates and routing decisions
- Validate trace recording

#### 2.2 Alert System Testing
- Test email alert functionality (Gmail SMTP)
- Verify Telegram alert sending
- Validate Slack webhook alerts
- Test custom webhook alerts

#### 2.3 Performance Testing
- Measure latency for each pipeline stage
- Test concurrent processing capability
- Verify memory usage during processing

### Phase 3: Accuracy and Evaluation

#### 3.1 RAG Accuracy Metrics
- Run retrieval evaluation scripts
- Test classification accuracy
- Evaluate draft quality metrics
- Compare with baseline performance

#### 3.2 Dataset Validation
- Verify dataset generation completeness
- Check for data leakage issues
- Validate test/train/validation splits

### Phase 4: Production Readiness

#### 4.1 Configuration Management
- Test .env configuration loading
- Verify config file validation
- Check provider priority configuration

#### 4.2 Error Handling and Resilience
- Test provider failover scenarios
- Validate circuit breaker behavior
- Verify graceful degradation

## Specific Verification Steps

### Step 1: Environment Setup
```bash
# Setup and initial configuration
cp .env.example .env
uv sync
ollama pull qwen2.5:7b-instruct
make setup  # This should run uv sync + alembic upgrade head + ollama pull
```

### Step 2: Core Component Testing
```bash
# Test individual components
make test            # Run unit tests
make test-int        # Run integration tests
make check           # Run full lint/typecheck

# Test specific modules
uv run python -c "
from taskflow.adapters.llm.openrouter import OpenRouterProvider
from taskflow.adapters.llm.claude import ClaudeProvider  
from taskflow.adapters.llm.ollama import OllamaProvider
from taskflow.adapters.vector.qdrant_store import QdrantVectorStore
from taskflow.services.classify.service import classify_intent
print('✓ All core components imported successfully')
"
```

### Step 3: Functional Testing
```bash
# Test the full pipeline with a sample message
uv run python scripts/run_scenario.py --text "I need a refund for unused licenses"

# Test with the verification script
uv run python scripts/verify_system.py

# Test live sender functionality
uv run python scripts/test_live_sender.py
```

### Step 4: Accuracy and Evaluation
```bash
# Run evaluation scripts
uv run python scripts/eval_classifier.py
uv run python scripts/eval_retrieval.py
uv run python scripts/eval_drafting.py

# Check dataset integrity
uv run python scripts/check_leakage.py
```

## Expected Results for Each Component

### 1. OpenRouter API Integration
- ✅ Successfully connects with valid API key
- ✅ Parses structured JSON responses correctly
- ✅ Handles API errors gracefully
- ✅ Falls back to Claude when OpenRouter fails

### 2. Local LLM Integration
- ✅ Ollama service running and responsive
- ✅ qwen2.5:7b-instruct model loaded
- ✅ Generates responses offline
- ✅ Handles structured output requirements

### 3. Vector Database
- ✅ Qdrant initialized at ./data/qdrant
- ✅ FastEmbed models loaded (BAAI/bge-small-en-v1.5, Qdrant/bm25)
- ✅ Embedding generation working
- ✅ Hybrid search returning results
- ✅ Collection indexing functional

### 4. Intent Classification
- ✅ Accurately classifies common support queries
- ✅ Confidence scores meaningful (0.0-1.0 range)
- ✅ Fallback mechanism works for unknown queries
- ✅ Consistent output format

### 5. Draft Generation
- ✅ Context-grounded responses generated
- ✅ Citations included in proper format [chunk_id]
- ✅ Tone and complexity appropriate
- ✅ Fallback draft generated when LLM fails

### 6. Alert System
- ✅ Email alerts sent via Gmail SMTP
- ✅ Telegram notifications delivered
- ✅ Slack webhooks processed
- ✅ Custom webhooks functional

## Testing Strategy

### Unit Tests
- Individual component tests (LLM providers, vector store, services)
- Mocked external dependencies
- Edge case handling verification

### Integration Tests  
- End-to-end pipeline testing
- Database interaction verification
- Network service connectivity

### System Tests
- Complete demo scenarios
- Performance benchmarking
- Error condition simulation

## Risk Mitigation

1. **Missing Dependencies**: Ensure all required packages are in pyproject.toml
2. **Configuration Issues**: Verify .env and config files are properly loaded
3. **Offline Mode Limitations**: Test local-only mode thoroughly
4. **Model Availability**: Ensure Ollama model is pulled before testing

## Next Steps

1. Execute Phase 1 verification tests
2. Run integration tests to confirm all components work together
3. Perform accuracy evaluation with real-world scenarios
4. Document findings and create improvement plan for missing components