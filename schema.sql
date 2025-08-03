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
    source_name TEXT UNIQUE, -- Змінено з NOT NULL
    source_url TEXT UNIQUE NOT NULL,
    source_category TEXT,
    source_language TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    last_parsed TIMESTAMP WITH TIME ZONE DEFAULT '1970-01-01 00:00:00+00',
    parser_settings JSONB DEFAULT '{}'::jsonb
);

-- Додаємо колонку is_active, якщо вона не існує
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='sources' AND column_name='is_active') THEN
        ALTER TABLE sources ADD COLUMN is_active BOOLEAN DEFAULT TRUE;
        RAISE NOTICE 'Added column is_active to table sources';
    END IF;
END $$;

-- Таблиця новин
CREATE TABLE IF NOT EXISTS news (
    id SERIAL PRIMARY KEY,
    source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    title TEXT,
    content TEXT,
    source_url TEXT UNIQUE NOT NULL,
    image_url TEXT,
    published_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    is_sent BOOLEAN DEFAULT FALSE,
    summary TEXT, -- Резюме новини від AI
    translation TEXT, -- Переклад новини від AI
    ai_translated BOOLEAN DEFAULT FALSE
);


-- Таблиця підписок користувачів
CREATE TABLE IF NOT EXISTS user_subscriptions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    subscribed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, source_id)
);

-- Таблиця для відстеження статистики парсингу джерел
CREATE TABLE IF NOT EXISTS source_stats (
    id SERIAL PRIMARY KEY,
    source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    parsed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    new_articles_count INTEGER DEFAULT 0,
    parse_duration_ms INTEGER,
    status TEXT -- 'success', 'failure'
);

-- Таблиця для черги завдань (наприклад, для відкладеного парсингу або розсилки дайджестів)
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

-- Додавання початкових джерел, якщо вони ще не існують
-- Це може бути причиною помилки, якщо `is_active` не існує в таблиці
INSERT INTO sources (source_name, source_url, source_language, is_active) VALUES
('Українська правда', 'https://www.pravda.com.ua/news/', 'uk', TRUE),
('Бабель', 'https://babel.ua/news', 'uk', TRUE)
ON CONFLICT (source_url) DO NOTHING;
