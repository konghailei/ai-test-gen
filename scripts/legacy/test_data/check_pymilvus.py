"""Check pymilvus version and Hit/Entity API."""
import pymilvus
print(f"pymilvus version: {pymilvus.__version__}")

# Check if Hit is available
try:
    from pymilvus.client.types import Hit
    hit_methods = [m for m in dir(Hit) if not m.startswith("_")]
    print(f"Hit methods: {hit_methods}")
except ImportError:
    print("Hit not directly importable")

# Check Entity
try:
    from pymilvus.client.types import Entity
    entity_methods = [m for m in dir(Entity) if not m.startswith("_")]
    print(f"Entity methods: {entity_methods}")
except ImportError:
    print("Entity not directly importable")

# Check the search result structure by looking at the SearchResult
try:
    from pymilvus.client.search_result import SearchResult, Hit
    hit_methods = [m for m in dir(Hit) if not m.startswith("_")]
    print(f"Hit (from search_result) methods: {hit_methods}")
except ImportError:
    pass

# Check if Hit has entity property
try:
    from pymilvus.client.search_result import Hit
    import inspect
    # Get the source of the get method
    if hasattr(Hit, "get"):
        print(f"\nHit.get signature: {inspect.signature(Hit.get)}")
    if hasattr(Hit, "entity"):
        print("Hit has 'entity' property")
        # Check entity type
        hit_src = inspect.getsource(Hit)
        # Find the entity property definition
        for line in hit_src.split("\n"):
            if "entity" in line.lower() or "def get" in line.lower():
                print(f"  {line.strip()}")
except Exception as e:
    print(f"Error inspecting Hit: {e}")
