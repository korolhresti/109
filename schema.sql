-- Таблиця користувачів
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    is_admin BOOLEAN DEFAULT FALSE,
    last_active TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    language TEXT DEFAULT 'uk',
    auto_notifications BOOLEAN DEFAULT FALSE,
    digest_frequency TEXT DEFAULT 'daily',
    safe_mode BOOLEAN DEFAULT FALSE,
    current_feed_id INTEGER, -- Може бути NULL
    is_premium BOOLEAN DEFAULT FALSE,
    premium_expires_at TIMESTAMP WITH TIME ZONE, -- Може бути NULL
    level INTEGER DEFAULT 1,
    badges JSONB DEFAULT '[]'::jsonb, -- Зберігаємо як JSONB для гнучкості
    inviter_id INTEGER REFERENCES users(id) ON DELETE SET NULL, -- Може бути NULL
    view_mode TEXT DEFAULT 'detailed',
    premium_invite_count INTEGER DEFAULT 0,
    digest_invite_count INTEGER DEFAULT 0,
    is_pro BOOLEAN DEFAULT FALSE,
    ai_requests_today INTEGER DEFAULT 0,
    ai_last_request_date DATE DEFAULT CURRENT_DATE,
    preferred_language TEXT DEFAULT 'uk' -- НОВА КОЛОНКА: мова для перекладу новин
);

-- Таблиця джерел новин
CREATE TABLE IF NOT EXISTS sources (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT UNIQUE NOT NULL,
    category TEXT NOT NULL,
    language TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'active',
    last_parsed_at TIMESTAMP WITH TIME ZONE,
    last_error TEXT,
    original_url TEXT, -- NEW COLUMN: to store the original URL if redirects occur
    source_name TEXT, -- NEW COLUMN: to store the name from RSS/site if different from 'name'
    source_category TEXT, -- NEW COLUMN: to store the category from RSS/site
    source_language TEXT, -- NEW COLUMN: to store the language from RSS/site
    feed_url TEXT UNIQUE,
    parse_interval_minutes INTEGER DEFAULT 60
);

-- Додаємо унікальний індекс на URL, якщо він ще не існує
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'sources_url_unique') THEN
        ALTER TABLE sources ADD CONSTRAINT sources_url_unique UNIQUE (url);
    END IF;
END $$;

-- Додаємо або оновлюємо стовпці з IF NOT EXISTS для уникнення помилок
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='sources' AND column_name='status') THEN
        ALTER TABLE sources ADD COLUMN status TEXT DEFAULT 'active';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='sources' AND column_name='last_parsed_at') THEN
        ALTER TABLE sources ADD COLUMN last_parsed_at TIMESTAMP WITH TIME ZONE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='sources' AND column_name='parse_interval_minutes') THEN
        ALTER TABLE sources ADD COLUMN parse_interval_minutes INTEGER DEFAULT 60;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='sources' AND column_name='original_url') THEN
        ALTER TABLE sources ADD COLUMN original_url TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='sources' AND column_name='source_name') THEN
        ALTER TABLE sources ADD COLUMN source_name TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='sources' AND column_name='source_category') THEN
        ALTER TABLE sources ADD COLUMN source_category TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='sources' AND column_name='source_language') THEN
        ALTER TABLE sources ADD COLUMN source_language TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='sources' AND column_name='feed_url') THEN
        ALTER TABLE sources ADD COLUMN feed_url TEXT UNIQUE;
    END IF;
END $$;

-- Таблиця новин
CREATE TABLE IF NOT EXISTS news (
    id SERIAL PRIMARY KEY,
    source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE NOT NULL,
    title TEXT NOT NULL,
    content TEXT,
    source_url TEXT UNIQUE NOT NULL,
    image_url TEXT,
    published_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    lang TEXT DEFAULT 'uk',
    is_sent BOOLEAN DEFAULT FALSE -- NEW COLUMN: to track if news has been sent to channel
);

-- Таблиця переглядів новин користувачами
CREATE TABLE IF NOT EXISTS user_news_views (
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE NOT NULL,
    news_id INTEGER REFERENCES news(id) ON DELETE CASCADE NOT NULL,
    viewed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, news_id)
);

-- Таблиця реакцій користувачів на новини
CREATE TABLE IF NOT EXISTS user_news_reactions (
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE NOT NULL,
    news_id INTEGER REFERENCES news(id) ON DELETE CASCADE NOT NULL,
    reaction TEXT NOT NULL, -- 'like', 'dislike', 'bookmark', etc.
    reacted_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, news_id)
);

-- Таблиця закладок
CREATE TABLE IF NOT EXISTS bookmarks (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE NOT NULL,
    news_id INTEGER REFERENCES news(id) ON DELETE CASCADE NOT NULL,
    bookmarked_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, news_id)
);

-- Таблиця звітів про новини/джерела
CREATE TABLE IF NOT EXISTS reports (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    report_type TEXT NOT NULL, -- 'news_error', 'source_issue', 'other'
    report_data JSONB, -- Додаткові дані про звіт
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'pending' -- 'pending', 'reviewed', 'resolved'
);

-- Таблиця запрошень
CREATE TABLE IF NOT EXISTS invitations (
    id SERIAL PRIMARY KEY,
    inviter_id INTEGER REFERENCES users(id) ON DELETE CASCADE NOT NULL,
    invite_code TEXT UNIQUE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP WITH TIME ZONE,
    max_uses INTEGER DEFAULT 1,
    current_uses INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE
);

-- Таблиця статистики джерел
CREATE TABLE IF NOT EXISTS source_stats (
    source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE NOT NULL,
    total_news_parsed INTEGER DEFAULT 0,
    last_24h_news INTEGER DEFAULT 0,
    last_7d_news INTEGER DEFAULT 0,
    last_update TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_id)
);

-- Таблиця підписок користувачів на джерела
CREATE TABLE IF NOT EXISTS user_subscriptions (
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE NOT NULL,
    source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE NOT NULL,
    subscribed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, source_id)
);

-- Таблиця для кешування RSS-стрічок
CREATE TABLE IF NOT EXISTS rss_cache (
    feed_url TEXT PRIMARY KEY,
    last_fetched TIMESTAMP WITH TIME ZONE,
    content TEXT,
    etag TEXT,
    last_modified TEXT
);

-- Таблиця черги завдань
CREATE TABLE IF NOT EXISTS task_queue (
    id SERIAL PRIMARY KEY,
    task_type TEXT NOT NULL, -- 'parse_source', 'send_digest', etc.
    task_data JSONB NOT NULL, -- Дані, необхідні для виконання завдання
    scheduled_at TIMESTAMP WITH TIME ZONE NOT NULL,
    status TEXT DEFAULT 'pending', -- 'pending', 'processing', 'completed', 'failed'
    processed_at TIMESTAMP WITH TIME ZONE,
    error_message TEXT
);

-- Таблиця для зберігання налаштувань бота
CREATE TABLE IF NOT EXISTS bot_settings (
    id SERIAL PRIMARY KEY,
    setting_key TEXT UNIQUE NOT NULL,
    setting_value TEXT NOT NULL,
    description TEXT
);

-- Додавання початкових налаштувань, якщо вони ще не існують
INSERT INTO bot_settings (setting_key, setting_value, description) VALUES
('DEFAULT_PARSE_INTERVAL_MINUTES', '60', 'Інтервал парсингу за замовчуванням для нових джерел')
ON CONFLICT (setting_key) DO NOTHING;

INSERT INTO bot_settings (setting_key, setting_value, description) VALUES
('MAX_AI_REQUESTS_PER_DAY', '5', 'Максимальна кількість AI-запитів на день для звичайних користувачів')
ON CONFLICT (setting_key) DO NOTHING;

INSERT INTO bot_settings (setting_key, setting_value, description) VALUES
('NEWS_PUBLISH_INTERVAL_MINUTES', '5', 'Інтервал публікації новин в канал')
ON CONFLICT (setting_key) DO NOTHING;

INSERT INTO bot_settings (setting_key, setting_value, description) VALUES
('NEWS_PARSE_INTERVAL_MINUTES', '15', 'Інтервал парсингу новин для всіх активних джерел')
ON CONFLICT (setting_key) DO NOTHING;

-- Ensure source_name, source_category, source_language are nullable
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='sources' AND column_name='source_name' AND is_nullable='NO') THEN
        ALTER TABLE sources ALTER COLUMN source_name DROP NOT NULL;
        RAISE NOTICE 'Dropped NOT NULL constraint on sources.source_name';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='sources' AND column_name='source_category' AND is_nullable='NO') THEN
        ALTER TABLE sources ALTER COLUMN source_category DROP NOT NULL;
        RAISE NOTICE 'Dropped NOT NULL constraint on sources.source_category';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='sources' AND column_name='source_language' AND is_nullable='NO') THEN
        ALTER TABLE sources ALTER COLUMN source_language DROP NOT NULL;
        RAISE NOTICE 'Dropped NOT NULL constraint on sources.source_language';
    END IF;
END $$;
