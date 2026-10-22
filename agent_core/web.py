"""Optional stock web research tools share a server-owned plugin boundary."""
import re


def initial_search_tool(runtime, messages):
    if not getattr(runtime, 'body', {}).get('web_enabled'):
        return None
    question = next((m.get('content', '') for m in reversed(messages) if m.get('role') == 'user'), '')
    if not isinstance(question, str) or not re.search(
        r'(?:当前|实时|最新|今日|现在).{0,8}(?:股价|价格|报价|现价|价)|(?:联网|网上|搜索)', question
    ):
        return None
    return next((t['function']['name'] for t in runtime.tools
                 if t['function']['name'] in ('stock_web_search', 'mcp__stock-web__stock_web_search')), None)
def is_web_tool(entry):
    return entry.get('pluginId') == 'stock-web' or entry.get('_mcpServer', {}).get('pluginId') == 'stock-web'


def select_tools(entries, names, enabled):
    if not enabled:
        return [name for name in names if not is_web_tool(entries[name])]
    return names
