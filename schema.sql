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
    source_name TEXT NOT NULL,
    source_url TEXT UNIQUE NOT NULL,
    source_category TEXT,
    source_language TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    last_parsed TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Таблиця новин
CREATE TABLE IF NOT EXISTS news (
    id SERIAL PRIMARY KEY,
    source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    content TEXT,
    image_url TEXT,
    source_url TEXT UNIQUE NOT NULL,
    published_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    is_sent BOOLEAN DEFAULT FALSE,
    sent_at TIMESTAMP WITH TIME ZONE
);

-- Таблиця підписок користувачів
CREATE TABLE IF NOT EXISTS user_subscriptions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    subscribed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, source_id)
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
('NEWS_PARSE_INTERVAL_MINUTES', '15', 'Інтервал парсингу новин для всіх активних джерел'),
('NEWS_PUBLISH_INTERVAL_MINUTES', '5', 'Інтервал публікації новин в канал')
ON CONFLICT (setting_key) DO NOTHING;

-- Додавання тестових джерел, якщо їх ще немає
INSERT INTO sources (source_name, source_url, source_language, is_active) VALUES
('Європейська правда', 'https://www.eurointegration.com.ua/news/', 'uk', TRUE),
('Мінпром', 'https://minprom.ua/news/', 'uk', TRUE)
ON CONFLICT (source_url) DO NOTHING;
