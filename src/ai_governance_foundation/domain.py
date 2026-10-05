"""定义基础服务允许登记的资料类别。"""

ALLOWED_CATEGORIES = frozenset({
    "institution_profile",
    "innovation_node_registry",
    "research_resource",
    "project_assignment",
})


def is_allowed_category(value: str) -> bool:
    return value in ALLOWED_CATEGORIES
