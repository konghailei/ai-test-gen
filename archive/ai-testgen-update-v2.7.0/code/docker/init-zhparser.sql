-- zhparser 中文分词扩展初始化
-- zhparser 不可用时静默跳过，系统降级为 simple 文本搜索配置
-- （hybrid_searcher._get_ts_config 运行时检测并缓存可用配置）

DO $$
BEGIN
    -- 尝试创建 zhparser 扩展
    BEGIN
        CREATE EXTENSION IF NOT EXISTS zhparser;
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'zhparser 扩展不可用，BM25 将使用 simple 文本搜索配置';
        RETURN;
    END;

    -- 创建中文全文搜索配置（如已存在则跳过）
    BEGIN
        CREATE TEXT SEARCH CONFIGURATION chinese (PARSER = zhparser);
    EXCEPTION WHEN duplicate_object THEN
        NULL;
    END;

    -- 配置词性映射（名词/动词/形容词/习语/叹词/惯用语 -> simple）
    BEGIN
        ALTER TEXT SEARCH CONFIGURATION chinese ADD MAPPING FOR n,v,a,i,e,l WITH simple;
    EXCEPTION WHEN OTHERS THEN
        NULL;
    END;

    RAISE NOTICE 'zhparser 中文分词扩展已就绪';
END
$$;
