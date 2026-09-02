# Summary of Plumbus Code

## Overview
Plumbus Code is a modular terminal coding agent designed for single-user environments, implementing a structured agent architecture similar to PI's inner workings. It operates using core functionalities, a provider abstraction, tools for code interaction, a permissions system, and durability/storage options.

## Overview
Plumbus Code is a modular terminal coding agent designed for single-user environments, implementing a structured agent architecture similar to PI's inner workings. It is composed of core functionalities, a provider abstraction, tools for code interaction, a permissions system, and options for durable storage and resumability.

## Core Components
### 1. **Agent:** 
   The central orchestration and execution logic managing the flow of commands and tool interactions.
### 2. **AI Layer:**
   Encapsulates provider implementations (e.g., OpenRouterProvider) which connect to external AI services and handle request/response cycles. 
### 3. **Tools:** 
   A collection of tools (e.g., BashTool, ReadFileTool) enabling interaction with the filesystem and executing commands.
- **Agent:** The central orchestration and execution logic managing the flow of commands and tool interactions.
- **AI Layer:** Encapsulates provider implementations (e.g., OpenRouterProvider) which connect to external AI services and handle request/response cycles.
- **Tools:** A collection of tools (e.g., BashTool, ReadFileTool) enabling interaction with the filesystem and executing commands.
- **Permissions:** Enforces rules about which tools can be used in which contexts, preventing potentially dangerous operations.
- **Storage:** Manages persistent and ephemeral data across sessions, ensuring recovery and state management.

### Functionalities
##### 2. **Provider Interfaces:** 
   Uniform interface for different AI provider implementations, currently only using OpenRouter.Provider. The provider is defined as follows:
   ```python
   class Provider(Protocol):
       def stream(self, model: Model, context: Context, options: StreamOptions) -> AsyncIterator[StreamEvent]:
           ...
   ``` 
   Uniform interface for different AI provider implementations, currently only using OpenRouter.Provider. The provider is defined as follows:
   ```python
   class Provider(Protocol):
       def stream(self, model: Model, context: Context, options: StreamOptions) -> AsyncIterator[StreamEvent]:
           ...
   ``` 
   The loop handles the turn lifecycle, including managing tool calls and responses while ensuring proper state management. Here is a key function from agent/loop.py:
   ```python
   async def run_loop_stream(...):
       for turn_idx in range(max_turns):
           yield AgentTurnStart(turn=turn_idx)
           ...
   ```
### 1. **Agent Loop: 
   The loop handles the turn lifecycle, including managing tool calls and responses while ensuring proper state management.**
### 2. **Provider Interfaces:**
   Uniform interface for different AI provider implementations, currently only using OpenRouter.Provider.
#### 4. **Permissions Gates:**
   Evaluates rules about which tools can be used in which contexts, preventing potentially dangerous operations. A basic structure is:
   ```python
   class PermissionPolicy:
       def evaluate(self, tool: Tool, arguments: dict[str, Any]) -> ActionPolicy:
           ...
   ```
   Evaluates rules about which tools can be used in which contexts, preventing potentially dangerous operations. A basic structure is:
   ```python
   class PermissionPolicy:
       def evaluate(self, tool: Tool, arguments: dict[str, Any]) -> ActionPolicy:
           ...
   ```
   Base class that tools extend — includes managing tool context, executing commands in a controlled manner.
### 4. **Permissions Gates:**
   Evaluates if a tool call is safe to execute, requiring user confirmation when necessary.
### 5. **Storage Management:**
   Includes an in-memory and SQLite backend, ensuring atomic transactions with rollback capability.

## Closing Note
As development progresses, future phases will incorporate features such as true concurrency, advanced permissions handling, and refined state management, ultimately leading to a fully interactive terminal agent capable of complex tasks. Additional implementation details, such as how tools interact with the filesystem safely, are elaborated in the respective tool files.  
As development progresses, future phases will incorporate features such as true concurrency, advanced permissions handling, and refined state management, ultimately leading to a fully interactive terminal agent capable of complex tasks.
