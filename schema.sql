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
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, -- Може бути NULL для системних джерел
    source_type TEXT NOT NULL, -- 'rss', 'web', 'telegram', 'social_media'
    feed_url TEXT UNIQUE, -- Для RSS
    name TEXT NOT NULL,
    last_parsed_at TIMESTAMP WITH TIME ZONE,
    status TEXT DEFAULT 'active', -- 'active', 'inactive', 'error'
    error_message TEXT,
    parser_config JSONB DEFAULT '{}'::jsonb, -- Додаткові налаштування для парсера
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    is_public BOOLEAN DEFAULT FALSE,
    language TEXT DEFAULT 'uk',
    parse_interval_minutes INTEGER DEFAULT 60 -- Інтервал парсингу в хвилинах
);

-- Таблиця новин
CREATE TABLE IF NOT EXISTS news (
    id SERIAL PRIMARY KEY,
    source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    content TEXT,
    source_url TEXT UNIQUE NOT NULL,
    normalized_source_url TEXT UNIQUE NOT NULL,
    image_url TEXT,
    published_at TIMESTAMP WITH TIME ZONE,
    moderation_status TEXT DEFAULT 'pending', -- 'pending', 'approved', 'rejected'
    expires_at TIMESTAMP WITH TIME ZONE,
    is_published_to_channel BOOLEAN DEFAULT FALSE,
    ai_classified_topics JSONB DEFAULT '[]'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Таблиця переглядів новин користувачами
CREATE TABLE IF NOT EXISTS user_news_views (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    news_id INTEGER REFERENCES news(id) ON DELETE CASCADE,
    viewed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, news_id) -- Кожен користувач може переглянути новину лише один раз
);

-- Таблиця реакцій користувачів на новини
CREATE TABLE IF NOT EXISTS user_news_reactions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    news_id INTEGER REFERENCES news(id) ON DELETE CASCADE,
    reaction_type TEXT NOT NULL, -- 'like', 'dislike', 'bookmark', 'share', etc.
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, news_id, reaction_type) -- Кожен користувач може мати лише одну реакцію певного типу на новину
);

-- Таблиця закладок
CREATE TABLE IF NOT EXISTS bookmarks (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    news_id INTEGER REFERENCES news(id) ON DELETE CASCADE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, news_id)
);

-- Таблиця скарг на контент
CREATE TABLE IF NOT EXISTS reports (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL, -- 'news', 'comment', etc.
    target_id INTEGER NOT NULL,
    reason TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'pending' -- 'pending', 'resolved', 'rejected'
);

-- Таблиця запрошень
CREATE TABLE IF NOT EXISTS invitations (
    id SERIAL PRIMARY KEY,
    inviter_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    invite_code VARCHAR(8) UNIQUE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    used_at TIMESTAMP WITH TIME ZONE, -- Може бути NULL, якщо запрошення ще не використано
    status TEXT DEFAULT 'pending', -- 'pending', 'accepted', 'revoked'
    invitee_telegram_id BIGINT REFERENCES users(telegram_id) ON DELETE SET NULL -- Змінено на telegram_id
);

-- Таблиця статистики джерел
CREATE TABLE IF NOT EXISTS source_stats (
    source_id INTEGER PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
    publication_count INTEGER DEFAULT 0,
    last_updated TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Таблиця підписок користувачів на теми
CREATE TABLE IF NOT EXISTS user_subscriptions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    topic TEXT NOT NULL,
    subscribed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, topic)
);

-- Таблиця кешу для RSS-фіду
CREATE TABLE IF NOT EXISTS rss_cache (
    id SERIAL PRIMARY KEY,
    feed_url TEXT UNIQUE NOT NULL,
    etag TEXT,
    last_modified TEXT,
    cached_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Таблиця для зберігання черги завдань (наприклад, для відкладеного парсингу)
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
('PREMIUM_MAX_AI_REQUESTS_PER_DAY', '50', 'Максимальна кількість AI-запитів на день для преміум-користувачів')
ON CONFLICT (setting_key) DO NOTHING;
