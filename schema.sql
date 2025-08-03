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
    current_feed_id INTEGER,
    is_premium BOOLEAN DEFAULT FALSE,
    premium_expires_at TIMESTAMP WITH TIME ZONE,
    level INTEGER DEFAULT 1,
    badges JSONB DEFAULT '[]'::jsonb,
    inviter_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    view_mode TEXT DEFAULT 'detailed',
    premium_invite_count INTEGER DEFAULT 0,
    digest_invite_count INTEGER DEFAULT 0,
    is_pro BOOLEAN DEFAULT FALSE,
    ai_requests_today INTEGER DEFAULT 0,
    ai_last_request_date DATE DEFAULT CURRENT_DATE,
    preferred_language TEXT DEFAULT 'uk'
);

-- Таблиця джерел новин
CREATE TABLE IF NOT EXISTS sources (
    id SERIAL PRIMARY KEY,
    source_name TEXT,
    source_url TEXT UNIQUE NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'web', -- ДОДАНО НОВЕ ПОЛЕ
    source_language TEXT,
    source_category TEXT,
    last_parsed TIMESTAMP WITH TIME ZONE,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Таблиця новин
CREATE TABLE IF NOT EXISTS news (
    id SERIAL PRIMARY KEY,
    source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    content TEXT,
    source_url TEXT UNIQUE NOT NULL,
    image_url TEXT,
    published_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    is_sent BOOLEAN DEFAULT FALSE,
    ai_summary TEXT,
    ai_classified_topics JSONB DEFAULT '[]'::jsonb,
    moderation_status TEXT DEFAULT 'pending',
    expires_at TIMESTAMP WITH TIME ZONE,
    is_published_to_channel BOOLEAN DEFAULT FALSE
);

-- Таблиця налаштувань бота
CREATE TABLE IF NOT EXISTS bot_settings (
    id SERIAL PRIMARY KEY,
    setting_key TEXT UNIQUE NOT NULL,
    setting_value TEXT NOT NULL,
    description TEXT
);

-- Таблиця для модерації
CREATE TABLE IF NOT EXISTS moderation_logs (
    id SERIAL PRIMARY KEY,
    news_id INTEGER REFERENCES news(id) ON DELETE SET NULL,
    moderator_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    reason TEXT,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Таблиця для підписок
CREATE TABLE IF NOT EXISTS subscriptions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Додавання початкових налаштувань, якщо вони ще не існують
INSERT INTO bot_settings (setting_key, setting_value, description) VALUES
('PUBLISH_INTERVAL_MINUTES', '10', 'Інтервал публікації новин в каналі'),
('PARSE_INTERVAL_MINUTES', '15', 'Інтервал парсингу новин для всіх активних джерел')
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
