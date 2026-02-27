//+------------------------------------------------------------------+
//|                        FN_Config.mqh                              |
//|        FundedNext EURUSD EA - Configuration & Types               |
//|        All input parameters, enums, and structs                   |
//+------------------------------------------------------------------+
#ifndef FN_CONFIG_MQH
#define FN_CONFIG_MQH

//--- Risk State Enum
enum ENUM_RISK_STATE
{
   RISK_NORMAL,         // Normal trading
   RISK_REDUCED,        // Reduced risk (after consecutive losses)
   RISK_DAILY_PAUSE,    // Paused for rest of day
   RISK_WEEKLY_PAUSE,   // Paused for rest of week
   RISK_KILL_SWITCH     // All trading disabled
};

//--- Signal Structure
struct SSignal
{
   int      direction;   // +1=BUY, -1=SELL, 0=NONE
   double   entry;
   double   sl;
   double   tp;
   double   slPips;
   double   tpPips;
   string   reason;

   void Reset()
   {
      direction = 0;
      entry     = 0;
      sl        = 0;
      tp        = 0;
      slPips    = 0;
      tpPips    = 0;
      reason    = "";
   }
};

//=== ACCOUNT ================================================================
input double Inp_InitialBalance     = 6000.0;    // Initial Balance ($)
input int    Inp_MagicNumber        = 202503;    // EA Magic Number

//=== RISK MANAGEMENT ========================================================
input double Inp_RiskPercent        = 1.5;       // Risk per trade (%)
input double Inp_RiskReduced        = 0.75;      // Reduced risk after losses (%)
input double Inp_MaxDailyLossPct    = 3.5;       // Internal daily loss limit (%) [prop=5%]
input double Inp_MaxTotalDDPct      = 8.0;       // Internal max DD (%) [prop=10%]
input bool   Inp_TrailingDDEnabled  = false;      // Enable trailing drawdown
input double Inp_TrailingDDPct      = 6.0;       // Trailing DD (%) if enabled
input int    Inp_MaxConsecLosses    = 5;         // Consecutive losses -> pause
input int    Inp_MaxTradesPerDay    = 8;         // Max trades per day

//=== SPREAD / SLIPPAGE ======================================================
input double Inp_MaxSpreadPips      = 2.5;       // Max spread to trade (pips)
input int    Inp_MaxSlippage        = 30;        // Max slippage (points)

//=== STRATEGY: EMA Trend Pullback ===========================================
input int    Inp_EMA_Fast           = 21;        // [H1] Fast EMA period
input int    Inp_EMA_Slow           = 50;        // [H4] Slow EMA period
input int    Inp_EMA_Trend          = 200;       // [H4] Trend EMA period
input int    Inp_RSI_Period         = 14;        // RSI period
input int    Inp_ATR_Period         = 14;        // ATR period
input int    Inp_ADX_Period         = 14;        // ADX period
input int    Inp_ADX_Threshold      = 25;        // Min ADX for valid trend
input double Inp_RR_Ratio           = 1.5;       // Risk:Reward ratio
input double Inp_RSI_BuyLow        = 38.0;      // RSI buy zone low
input double Inp_RSI_BuyHigh       = 52.0;      // RSI buy zone high
input double Inp_RSI_SellLow       = 48.0;      // RSI sell zone low
input double Inp_RSI_SellHigh      = 62.0;      // RSI sell zone high

//=== TRADE MANAGEMENT =======================================================
input bool   Inp_EnableBreakeven    = true;      // Enable breakeven
input double Inp_BreakevenR         = 1.0;       // Move SL to BE at R-multiple
input bool   Inp_EnableTrailing     = true;      // Enable trailing stop
input double Inp_TrailingStartR     = 1.5;       // Start trailing at R-multiple
input double Inp_TrailingATRMult    = 1.0;       // Trail distance (ATR x)
input bool   Inp_EnablePartialClose = false;      // Enable partial close
input double Inp_PartialCloseR     = 1.5;       // Partial close at R-multiple
input double Inp_PartialClosePct   = 50.0;      // Pct of position to close

//=== SESSION FILTER =========================================================
input bool   Inp_SessionFilterOn    = true;      // Enable session filter
input int    Inp_SessionStartHour   = 7;         // Session start (UTC hour)
input int    Inp_SessionEndHour     = 17;        // Session end (UTC hour)

//=== NEWS FILTER ============================================================
input bool   Inp_NewsFilterOn       = true;      // Enable news filter
input int    Inp_NewsPreMinutes     = 8;         // Minutes BEFORE news event
input int    Inp_NewsPostMinutes    = 8;         // Minutes AFTER news event
input bool   Inp_NewsCancelPending  = true;      // Cancel pendings before news
input bool   Inp_NewsMoveToBreakeven= true;      // Move open to BE before news

//=== LOGGING ================================================================
input bool   Inp_LogToFile          = true;      // Log trades to CSV
input bool   Inp_LogVerbose         = false;      // Verbose system logging

#endif
