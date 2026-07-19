"""验证导出文件结构。"""
import json
import zipfile

# 检查 JSON
with open('/app/test_output_json.json', encoding='utf-8') as f:
    data = json.load(f)
print('JSON 用例数:', len(data))
print('JSON 第一个用例 keys:', list(data[0].keys())[:15])
print('JSON 第一个用例 title:', data[0].get('title', '')[:60])

# 检查 XMind (zip 格式)
print()
print('XMind 文件验证:')
with zipfile.ZipFile('/app/test_output_xmind.xmind', 'r') as z:
    names = z.namelist()
    print('  内含文件:', names)
    if 'content.json' in names:
        with z.open('content.json') as f:
            content = json.load(f)
        print('  content.json 已解析')
        root = content[0].get('rootTopic', {})
        print('  根主题:', root.get('title', '')[:80])
        children = root.get('children', {}).get('attached', [])
        print('  一级子节点数:', len(children))
        if children:
            for i, ch in enumerate(children[:5]):
                print(f'    {i+1}. {ch.get("title", "")[:60]}')
                sub = ch.get('children', {}).get('attached', [])
                print(f'       子节点数: {len(sub)}')
    if 'metadata.json' in names:
        with z.open('metadata.json') as f:
            meta = json.load(f)
        print('  metadata:', meta)

# 检查 Markdown 行数
with open('/app/test_output_markdown.md', encoding='utf-8') as f:
    md = f.read()
print()
print('Markdown 文件:')
print('  总行数:', md.count('\n'))
print('  用例数（### 标题）:', md.count('\n### '))

# 检查 Gherkin
with open('/app/test_output_gherkin.feature', encoding='utf-8') as f:
    gherkin = f.read()
print()
print('Gherkin 文件:')
print('  Scenario 数:', gherkin.count('Scenario:'))
print('  Given 数:', gherkin.count('Given '))
print('  When 数:', gherkin.count('When '))
print('  Then 数:', gherkin.count('Then '))

# 检查 pytest
with open('/app/test_output_pytest.py', encoding='utf-8') as f:
    pytest_code = f.read()
print()
print('Pytest 文件:')
print('  def test_ 数:', pytest_code.count('def test_'))
print('  assert 数:', pytest_code.count('assert '))
print('  @pytest.mark 数:', pytest_code.count('@pytest.mark.'))
