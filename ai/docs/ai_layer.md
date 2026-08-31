# ai layer 

the ai layer is the connection layer for defining a detailed abstract layer and normalizing each model we add to have a structured stream 


## model.py
```
 class Model(Frozen):
    id: str                      # provider-specific id, e.g. "anthropic/claude-sonnet-4.5"
    provider: str                 # "openrouter"
    name: str                     # display name for UI/logs
    context_window: int
    supports_tools: bool = True
    supports_thinking: bool = False
    
```


