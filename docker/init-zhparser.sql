-- zhparser 中文分词扩展初始化
-- 安装 zhparser 扩展并配置中文全文搜索

CREATE EXTENSION IF NOT EXISTS zhparser;

-- 创建中文全文搜索配置
CREATE TEXT SEARCH CONFIGURATION chinese (PARSER = zhparser);

-- 配置词性映射（名词/动词/形容词/习语/叹词/惯用语 → simple）
ALTER TEXT SEARCH CONFIGURATION chinese ADD MAPPING FOR n,v,a,i,e,l WITH simple;
