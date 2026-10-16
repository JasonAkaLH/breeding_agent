from src.orchestration.models import CapabilityDescriptor, ExecutionInstance, InstanceState


KNOWLEDGE_CAPABILITY_DESCRIPTORS = tuple(
    CapabilityDescriptor(capability_id=capability_id, name=capability_id, description=description,
                         display_name=display_name, kind="knowledge", source="builtin")
    for capability_id, display_name, description in (
        ("knowledge.list_libraries", "文献库列表", "列出本轮固定范围内当前可读的文献库及描述，按描述路由；支持游标。"),
        ("knowledge.read_index", "读取文献索引", "读取指定文献库 Markdown 索引，选择相关文献；简述不能代替正文证据。支持游标。"),
        ("knowledge.read_document", "读取文献全文", "按文献 ID 阅读原始 Markdown 正文；跟随 next_cursor 连续阅读，未读完不可宣称全文已读。"),
    )
)


def build_local_knowledge_instance(*, instance_id: str = "inst-knowledge-local") -> ExecutionInstance:
    return ExecutionInstance(instance_id=instance_id,
                             supported_capabilities=tuple(item.capability_id for item in KNOWLEDGE_CAPABILITY_DESCRIPTORS),
                             state=InstanceState.ONLINE, load_score=0)
