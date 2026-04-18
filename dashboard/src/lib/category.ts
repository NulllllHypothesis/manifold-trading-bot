/**
 * Category inference — mirrors manifold_bot/strategies.py _CATEGORY_KEYWORDS
 * and _infer_market_category() exactly. Single source for dashboard code.
 */

const CATEGORY_KEYWORDS: Record<string, string[]> = {
  crypto: [
    "bitcoin", "btc", "ethereum", "crypto", "blockchain",
    "defi", "nft", "solana", "binance", "coinbase", "stablecoin",
    "doge", "dogecoin", "xrp", "ripple",
  ],
  ai_tech: [
    " ai ", "gpt", "llm", "openai", "anthropic", "claude", "gemini",
    "machine learning", "artificial intelligence", "neural", "deepmind",
    "chatgpt", "language model", "deepseek", "chatbot", "mistral",
    "grok", "xai",
  ],
  gaming: [
    " xbox ", " playstation ", " ps5 ", " ps6 ",
    " nintendo ", " switch ", " steam ",
    "video game", "videogame", "e-sport", "esport", "twitch",
    "minecraft", "fortnite", "roblox", "zelda", "mario",
    "pokemon", "pokémon", "call of duty", "warcraft",
    "league of legends", "valorant", "counter-strike",
    " dota ", " dota2", "gta ", " gta6", "starfield", "elden ring",
    "game release", "game launch", "gaming", "speedrun",
  ],
  entertainment: [
    "movie", "film release", "cinema", "box office",
    "oscar", "academy award", "emmy", "grammy",
    "golden globe", "tony award", "cannes",
    "netflix", "disney+", "disney plus", " hbo ",
    "streaming service", "amazon prime video", "paramount+",
    "tv show", "tv series", "sitcom", "drama series",
    "album", "concert", "tour dates", "billboard", "top 40",
    "music video",
    "celebrity", " celeb ", "hollywood", "actor", "actress",
    "taylor swift", "beyonce", "beyoncé", "drake", "kanye",
    "kardashian", "rihanna", "bieber",
    "marvel", "dc comics", "star wars", "harry potter",
    "lord of the rings", "game of thrones", "house of the dragon",
    "anime", "manga", "pixar", "dreamworks",
  ],
  politics: [
    "trump", "biden", "election", "congress", "senate", "president",
    "democrat", "republican", "vote", "policy", "legislation",
    "supreme court", "governor", "parliament",
    "war", "ceasefire", "military", "nato", "ukraine", "russia",
    "gaza", "israel", "iran", "harris", "political", "sanction",
    "tariff", "geopolit",
  ],
  sports: [
    " nba ", " nfl ", " mma ",
    "mlb", "nhl", "fifa", "ufc", "f1", "ncaa", "cricket",
    "tour de france", "tour of flanders", "giro d",
    "world cup", "olympics", "championship", "tennis", "golf",
    "soccer", "football", "basketball", "baseball", "premier league",
    " win ", " team ", " match ", " game ", " score ", " league ", " player ",
    "tournament", "bundesliga", "la liga", "serie a",
    "champions league", "europa league", "boxing",
    "formula 1", "wimbledon", "super bowl", "world series",
  ],
  science: [
    "nasa", "spacex", "climate", "vaccine", "fda", "cdc", "pandemic",
    "cancer", "physics", "biology", "crispr", "fusion",
    "earthquake", "hurricane", "temperature", "science", "research",
    "drug approval", "clinical trial",
    "artemis", "moon landing", "asteroid", "satellite launch",
  ],
  business: [
    " apple ", "google", "microsoft", " meta ", "facebook",
    "tesla", "nvidia", "tiktok", "twitter", " x corp", " xai ",
    "uber", "airbnb", "walmart", "mcdonalds", "mcdonald's",
    "starbucks", "nike", "boeing", "samsung", "sony",
    "spotify", "shopify", "amazon",
    "company", "corporation", "corporate",
    " ceo ", " cfo ", " cto ", "chief executive",
    "startup", " ipo ", "acquires", "acquisition", "merger",
    "bankruptcy", "product launch", "layoff", "layoffs",
    "earnings report", "quarterly report", "subscription",
  ],
  economics: [
    "gdp", "inflation", "federal reserve", "fed rate", "interest rate",
    "recession", "stock market", "nasdaq", "sp500", "s&p", "cpi",
    "unemployment", "treasury", "stock", "economy", "dow", "dollar",
    "euro", "trade deficit", "budget deficit", "debt ceiling",
  ],
}

export function inferCategory(question: string): string {
  const q = ` ${question.toLowerCase()} `
  for (const [category, keywords] of Object.entries(CATEGORY_KEYWORDS)) {
    for (const kw of keywords) {
      if (q.includes(kw)) return category
    }
  }
  return "other"
}
