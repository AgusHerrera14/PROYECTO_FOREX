//+------------------------------------------------------------------+
//|                     FN_RiskManager.mqh                            |
//|        FundedNext EURUSD EA - Risk Engine & DD Tracking          |
//+------------------------------------------------------------------+
#ifndef FN_RISK_MANAGER_MQH
#define FN_RISK_MANAGER_MQH

#include "FN_Config.mqh"
#include "FN_Utils.mqh"

class CRiskManager
{
private:
   // Configuration
   double   m_initialBalance;
   double   m_riskNormal;
   double   m_riskReduced;
   double   m_maxDailyLossPct;
   double   m_maxTotalDDPct;
   bool     m_trailingDDEnabled;
   double   m_trailingDDPct;
   int      m_maxConsecLosses;
   int      m_maxTradesPerDay;
   double   m_maxSpreadPips;

   // State tracking
   ENUM_RISK_STATE m_state;
   int      m_consecLosses;
   int      m_tradesToday;
   double   m_prevDayEODBalance;
   double   m_highWaterMark;       // For trailing DD
   int      m_currentDayOfYear;
   double   m_todayClosedPnL;

public:
   //+------------------------------------------------------------------+
   CRiskManager() : m_state(RISK_NORMAL), m_consecLosses(0), m_tradesToday(0),
                    m_prevDayEODBalance(0), m_highWaterMark(0),
                    m_currentDayOfYear(0), m_todayClosedPnL(0) {}

   //+------------------------------------------------------------------+
   void Init(double initBal, double riskNorm, double riskRed,
             double maxDailyPct, double maxDDPct,
             bool trailDD, double trailDDPct,
             int maxConsec, int maxTradesDay, double maxSpread)
   {
      m_initialBalance    = initBal;
      m_riskNormal        = riskNorm;
      m_riskReduced       = riskRed;
      m_maxDailyLossPct   = maxDailyPct;
      m_maxTotalDDPct     = maxDDPct;
      m_trailingDDEnabled = trailDD;
      m_trailingDDPct     = trailDDPct;
      m_maxConsecLosses   = maxConsec;
      m_maxTradesPerDay   = maxTradesDay;
      m_maxSpreadPips     = maxSpread;

      m_state             = RISK_NORMAL;
      m_consecLosses      = 0;
      m_tradesToday       = 0;
      m_todayClosedPnL    = 0;

      double bal = AccountInfoDouble(ACCOUNT_BALANCE);
      m_prevDayEODBalance = bal;
      m_highWaterMark     = bal;

      MqlDateTime dt;
      TimeCurrent(dt);
      m_currentDayOfYear = dt.day_of_year;

      Print("[FN-RISK] Initialized. Balance: ", DoubleToString(bal, 2),
            " | Daily limit: ", DoubleToString(m_maxDailyLossPct, 1), "%",
            " | Total DD limit: ", DoubleToString(m_maxTotalDDPct, 1), "%");
   }

   //+------------------------------------------------------------------+
   //| Main RuleCheck - call before every trade                         |
   //| Returns: "" if OK, or reason string if blocked                  |
   //+------------------------------------------------------------------+
   string RuleCheck()
   {
      UpdateState();

      // Kill switch
      if(m_state == RISK_KILL_SWITCH)
         return "KILL_SWITCH: Max DD exceeded";

      // Daily pause
      if(m_state == RISK_DAILY_PAUSE)
         return "DAILY_PAUSE: Daily loss limit or max trades reached";

      // Weekly pause
      if(m_state == RISK_WEEKLY_PAUSE)
         return "WEEKLY_PAUSE: Weekly loss limit reached";

      // Max trades per day
      if(m_tradesToday >= m_maxTradesPerDay)
         return "MAX_TRADES_DAY: " + IntegerToString(m_tradesToday) + " trades today";

      // Spread check
      if(!FN_IsSpreadOK(m_maxSpreadPips))
         return "SPREAD_HIGH: " + DoubleToString(FN_SpreadPips(), 1) + " pips";

      return "";  // All clear
   }

   //+------------------------------------------------------------------+
   //| Update risk state based on current account metrics               |
   //+------------------------------------------------------------------+
   void UpdateState()
   {
      CheckNewDay();

      double equity  = AccountInfoDouble(ACCOUNT_EQUITY);
      double balance = AccountInfoDouble(ACCOUNT_BALANCE);

      // Update high water mark for trailing DD
      if(balance > m_highWaterMark)
         m_highWaterMark = balance;

      // 1. Check Total DD from initial balance
      double totalDD = (m_initialBalance - equity) / m_initialBalance * 100.0;
      if(totalDD >= m_maxTotalDDPct)
      {
         m_state = RISK_KILL_SWITCH;
         Print("[FN-RISK] KILL SWITCH! Total DD: ", DoubleToString(totalDD, 2),
               "% >= ", DoubleToString(m_maxTotalDDPct, 1), "%");
         return;
      }

      // 2. Check Trailing DD (if enabled)
      if(m_trailingDDEnabled && m_highWaterMark > 0)
      {
         double trailDD = (m_highWaterMark - equity) / m_highWaterMark * 100.0;
         if(trailDD >= m_trailingDDPct)
         {
            m_state = RISK_KILL_SWITCH;
            Print("[FN-RISK] KILL SWITCH! Trailing DD: ", DoubleToString(trailDD, 2),
                  "% >= ", DoubleToString(m_trailingDDPct, 1), "%");
            return;
         }
      }

      // 3. Check Daily Loss (from previous day EOD balance)
      double dailyLimitDollars = m_prevDayEODBalance * m_maxDailyLossPct / 100.0;
      // Current day P&L = closed trades + floating
      double floatingPnL = equity - balance;
      double totalDayPnL = m_todayClosedPnL + floatingPnL;

      if(totalDayPnL < -dailyLimitDollars)
      {
         m_state = RISK_DAILY_PAUSE;
         Print("[FN-RISK] DAILY PAUSE! Day PnL: $", DoubleToString(totalDayPnL, 2),
               " | Limit: -$", DoubleToString(dailyLimitDollars, 2));
         return;
      }

      // 4. Check max trades
      if(m_tradesToday >= m_maxTradesPerDay)
      {
         m_state = RISK_DAILY_PAUSE;
         return;
      }

      // 5. Check consecutive losses
      if(m_consecLosses >= m_maxConsecLosses)
      {
         m_state = RISK_DAILY_PAUSE;
         Print("[FN-RISK] DAILY PAUSE! ", m_consecLosses, " consecutive losses");
         return;
      }

      // 6. Reduced risk after 3 consecutive losses
      if(m_consecLosses >= 3)
      {
         m_state = RISK_REDUCED;
         return;
      }

      m_state = RISK_NORMAL;
   }

   //+------------------------------------------------------------------+
   //| Calculate position size based on risk                             |
   //+------------------------------------------------------------------+
   double CalculateLotSize(double slPips)
   {
      if(slPips <= 0) return 0;

      double riskPct = GetCurrentRiskPercent();
      double balance = AccountInfoDouble(ACCOUNT_BALANCE);
      double riskDollars = balance * riskPct / 100.0;

      // Pip value for 1 standard lot (EURUSD ~ $10/pip)
      double tickValue = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
      double tickSize  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
      double pipSz     = FN_PipSize();

      if(tickSize <= 0 || tickValue <= 0 || pipSz <= 0) return 0;

      double pipValuePerLot = (pipSz / tickSize) * tickValue;
      if(pipValuePerLot <= 0) return 0;

      double lots = riskDollars / (slPips * pipValuePerLot);
      lots = FN_NormalizeLots(lots);

      return lots;
   }

   //+------------------------------------------------------------------+
   //| Notify: trade opened                                              |
   //+------------------------------------------------------------------+
   void OnTradeOpened()
   {
      m_tradesToday++;
   }

   //+------------------------------------------------------------------+
   //| Notify: trade closed with P&L                                     |
   //+------------------------------------------------------------------+
   void OnTradeClosed(double pnl)
   {
      m_todayClosedPnL += pnl;

      if(pnl < 0)
         m_consecLosses++;
      else if(pnl > 0)
         m_consecLosses = 0;

      // Update HWM
      double bal = AccountInfoDouble(ACCOUNT_BALANCE);
      if(bal > m_highWaterMark)
         m_highWaterMark = bal;
   }

   //+------------------------------------------------------------------+
   double GetCurrentRiskPercent()
   {
      return (m_state == RISK_REDUCED) ? m_riskReduced : m_riskNormal;
   }

   ENUM_RISK_STATE GetState()       { return m_state; }
   int             GetTradesToday() { return m_tradesToday; }
   int             GetConsecLosses(){ return m_consecLosses; }

   //+------------------------------------------------------------------+
   double GetDailyPnL()
   {
      double floatingPnL = AccountInfoDouble(ACCOUNT_EQUITY) - AccountInfoDouble(ACCOUNT_BALANCE);
      return m_todayClosedPnL + floatingPnL;
   }

   //+------------------------------------------------------------------+
   double GetTotalDDPercent()
   {
      double equity = AccountInfoDouble(ACCOUNT_EQUITY);
      if(m_initialBalance <= 0) return 0;
      return (m_initialBalance - equity) / m_initialBalance * 100.0;
   }

private:
   //+------------------------------------------------------------------+
   //| Reset daily counters at new day                                   |
   //+------------------------------------------------------------------+
   void CheckNewDay()
   {
      MqlDateTime dt;
      TimeCurrent(dt);

      if(dt.day_of_year != m_currentDayOfYear)
      {
         // New day: save EOD balance, reset counters
         m_prevDayEODBalance = AccountInfoDouble(ACCOUNT_BALANCE);
         m_todayClosedPnL    = 0;
         m_tradesToday       = 0;
         m_currentDayOfYear  = dt.day_of_year;

         // Reset daily/weekly pauses (but not kill switch)
         if(m_state == RISK_DAILY_PAUSE)
            m_state = (m_consecLosses >= 3) ? RISK_REDUCED : RISK_NORMAL;

         Print("[FN-RISK] New day. EOD Balance: $", DoubleToString(m_prevDayEODBalance, 2));
      }
   }
};

#endif
