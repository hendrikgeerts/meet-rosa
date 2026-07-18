"""Memory cards — vrije-tekst kennis die the user via iMessage aan
Rosa kan leren. Stored met semantic embedding voor fuzzy recall.

Public interface (zie tools.py voor tool-schemas):
- remember(text, tags?, links?, source?)
- recall(query, k?, tags?)
- list_memories(tags?, limit?, since?)
- forget_memory(memory_id)
"""
