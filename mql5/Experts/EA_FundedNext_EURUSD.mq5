//+------------------------------------------------------------------+
//|                    EA_FundedNext_EURUSD.mq5                      |
//|        FundedNext Prop Firm EURUSD Trading System v2.0           |
//|                                                                   |
//| Strategy: Multi-TF EMA Trend Pullback (H4 trend, H1 entry)     |
//| Risk:     FundedNext-compliant with conservative guardrails      |
//| News:     MQL5 Economic Calendar filter (disabled in Tester)    |
//| Logging:  CSV trade log + system log in MQL5/Files/             |
//|                                                                   |
//| TIMEFRAME: Attach to EURUSD H1 chart                             |
//| BROKER:    Any MT5 broker (designed for FundedNext)              |
//+------------------------------------------------------------------+
#property copyright "FundedNext EA v2.0"
#property version   "2.00"
#property description "FundedNext Stellar EURUSD - EMA Trend Pullback"
#property strict

//--- Include modules
#include <FundedNext/FN_Config.mqh>
#include <FundedNext/FN_Utils.mqh>
#include <FundedNext/FN_Logger.mqh>
#include <FundedNext/FN_SessionFilter.mqh>
#include <FundedNext/FN_NewsFilter.mqh>
#include <FundedNext/FN_RiskManager.mqh>
#include <FundedNext/FN_Strategy.mqh>
#include <FundedNext/FN_TradeManager.mqh>

//--- Global module instances
CLogger         g_Logger;
CNewsFilter     g_NewsFilter;
CRiskManager    g_RiskManager;
CStrategy       g_Strategy;
CTradeManager   g_TradeManager;

//--- State
datetime g_lastBarTime    = 0;
bool     g_initialized    = false;
ulong    g_lastDealTicket = 0;
int      g_heartbeatCount = 0;

//+------------------------------------------------------------------+
//| Expert initialization                                             |
//+------------------------------------------------------------------+
int OnInit()
{
   //--- Validate symbol
   string sym = _Symbol;
   if(StringFind(sym, "EURUSD") < 0)
   {
      Print("[FN-EA] WARNING: This EA is designed for EURUSD. Current symbol: ", sym);
      // Allow running on other symbols for testing, but warn
   }

   //--- Validate timeframe
   if(_Period != PERIOD_H1)
   {
      Print("[FN-EA] WARNING: Recommended timeframe is H1. Current: ", EnumToString(_Period));
   }

   //--- Initialize Logger
   if(!g_Logger.Init(Inp_LogToFile, Inp_LogVerbose))
   {
      Print("[FN-EA] Logger init failed, continuing without file logging");
   }

   g_Logger.Info("========== EA INITIALIZING ==========");
   g_Logger.Info("Symbol: " + _Symbol + " | TF: " + EnumToString(_Period));
   g_Logger.Info("Balance: $" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2));
   g_Logger.Info("Tester: " + (FN_IsTester() ? "YES" : "NO"));

   //--- Initialize News Filter
   g_NewsFilter.Init(Inp_NewsFilterOn, Inp_NewsPreMinutes, Inp_NewsPostMinutes,
                     Inp_NewsCancelPending, Inp_NewsMoveToBreakeven);

   //--- Initialize Risk Manager
   g_RiskManager.Init(Inp_InitialBalance, Inp_RiskPercent, Inp_RiskReduced,
                      Inp_MaxDailyLossPct, Inp_MaxTotalDDPct,
                      Inp_TrailingDDEnabled, Inp_TrailingDDPct,
                      Inp_MaxConsecLosses, Inp_MaxTradesPerDay, Inp_MaxSpreadPips);

   //--- Initialize Strategy
   if(!g_Strategy.Init(_Symbol,
                       Inp_EMA_Fast, Inp_EMA_Slow, Inp_EMA_Trend,
                       Inp_RSI_Period, Inp_ATR_Period, Inp_ADX_Period,
                       Inp_ADX_Threshold, Inp_RR_Ratio,
                       Inp_RSI_BuyLow, Inp_RSI_BuyHigh,
                       Inp_RSI_SellLow, Inp_RSI_SellHigh))
   {
      g_Logger.Error("Strategy init FAILED - check indicator handles");
      return INIT_FAILED;
   }

   //--- Initialize Trade Manager
   g_TradeManager.Init(Inp_MagicNumber, Inp_MaxSlippage,
                       Inp_EnableBreakeven, Inp_BreakevenR,
                       Inp_EnableTrailing, Inp_TrailingStartR, Inp_TrailingATRMult,
                       Inp_EnablePartialClose, Inp_PartialCloseR, Inp_PartialClosePct);

   //--- Set timer for periodic tasks (30 seconds)
   if(!FN_IsTester())
      EventSetTimer(30);

   g_Logger.Info("========== EA READY ==========");
   g_Logger.Info("Risk: " + DoubleToString(Inp_RiskPercent, 1) + "% | RR: " +
                 DoubleToString(Inp_RR_Ratio, 1) + " | MaxDD: " +
                 DoubleToString(Inp_MaxTotalDDPct, 1) + "%");
   g_Logger.Info("Session: " + (Inp_SessionFilterOn ?
                 IntegerToString(Inp_SessionStartHour) + "-" + IntegerToString(Inp_SessionEndHour) + " UTC"
                 : "OFF"));
   g_Logger.Info("News filter: " + (Inp_NewsFilterOn ? "ON" : "OFF"));

   g_initialized = true;
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Expert tick handler                                               |
//+------------------------------------------------------------------+
void OnTick()
{
   if(!g_initialized) return;

   //=== 1. MANAGE EXISTING POSITIONS (every tick) ===
   double atr = g_Strategy.GetATR();
   g_TradeManager.ManagePositions(atr);

   //=== 2. NEWS DEFENSIVE ACTIONS (every tick, live only) ===
   if(g_NewsFilter.IsNewsWindow())
   {
      // Cancel pending orders before news
      if(g_NewsFilter.ShouldCancelPendings())
         g_TradeManager.CancelAllPending("NEWS_WINDOW");

      // Move to breakeven if in profit
      if(g_NewsFilter.ShouldMoveToBreakeven())
         g_TradeManager.ForceBreakeven();
   }

   //=== 3. CHECK FOR NEW H1 BAR (signal processing only on bar close) ===
   if(!IsNewBar()) return;

   //=== 4. RISK STATE CHECK ===
   ENUM_RISK_STATE riskState = g_RiskManager.GetState();

   if(riskState == RISK_KILL_SWITCH)
   {
      g_TradeManager.CloseAllPositions("KILL_SWITCH");
      g_TradeManager.CancelAllPending("KILL_SWITCH");
      g_Logger.Error("KILL SWITCH ACTIVE - All trading stopped. DD: " +
                     DoubleToString(g_RiskManager.GetTotalDDPercent(), 2) + "%");
      return;
   }

   if(riskState == RISK_DAILY_PAUSE || riskState == RISK_WEEKLY_PAUSE)
   {
      g_Logger.Debug("Trading paused: " + EnumToString(riskState));
      return;
   }

   //=== 5. PRE-TRADE RULE CHECK ===
   string blockReason = g_RiskManager.RuleCheck();
   if(blockReason != "")
   {
      g_Logger.Debug("Trade blocked: " + blockReason);
      return;
   }

   //=== 6. SESSION FILTER ===
   if(!FN_IsActiveSession(Inp_SessionFilterOn, Inp_SessionStartHour, Inp_SessionEndHour))
   {
      g_Logger.Debug("Outside trading session");
      return;
   }

   //=== 7. NEWS FILTER (block new entries) ===
   if(g_NewsFilter.IsNewsWindow())
   {
      g_Logger.Debug("News window active: " + g_NewsFilter.GetStatusString());
      return;
   }

   //=== 8. ALREADY HAVE A POSITION? ===
   if(g_TradeManager.HasOurPosition())
      return;

   //=== 9. GENERATE SIGNAL ===
   SSignal signal;
   if(!g_Strategy.GetSignal(signal))
      return;

   //=== 10. CALCULATE POSITION SIZE ===
   double lots = g_RiskManager.CalculateLotSize(signal.slPips);
   if(lots <= 0)
   {
      g_Logger.Warning("Position size = 0. SL pips: " + DoubleToString(signal.slPips, 1));
      return;
   }

   //=== 11. FINAL SPREAD CHECK (just before execution) ===
   double spreadNow = FN_SpreadPips();
   if(spreadNow > Inp_MaxSpreadPips)
   {
      g_Logger.Debug("Spread too high at execution: " + DoubleToString(spreadNow, 1));
      return;
   }

   //=== 12. EXECUTE TRADE ===
   string stratName = "TrendPullback";
   bool success = g_TradeManager.ExecuteSignal(signal, lots, stratName);

   if(success)
   {
      g_RiskManager.OnTradeOpened();

      double riskPct = g_RiskManager.GetCurrentRiskPercent();
      string newsStatus = g_NewsFilter.GetStatusString();
      string typeStr = (signal.direction > 0) ? "BUY" : "SELL";

      g_Logger.LogTradeOpen(
         g_TradeManager.GetOpenTicket(),
         stratName,
         typeStr,
         signal.entry,
         signal.sl,
         signal.tp,
         lots,
         riskPct,
         spreadNow,
         newsStatus,
         signal.reason
      );

      g_Logger.Info(StringFormat("%s | %.2f lots | SL: %.1f pips | TP: %.1f pips | RR: %.1f | Spread: %.1f",
                    typeStr, lots, signal.slPips, signal.tpPips, Inp_RR_Ratio, spreadNow));
   }
}

//+------------------------------------------------------------------+
//| Timer handler (every 30 seconds, live only)                      |
//+------------------------------------------------------------------+
void OnTimer()
{
   // Refresh news cache
   g_NewsFilter.RefreshCache();

   // Update risk state
   g_RiskManager.UpdateState();

   // Hourly heartbeat
   g_heartbeatCount++;
   if(g_heartbeatCount >= 120)  // 120 x 30s = 1 hour
   {
      double bal = AccountInfoDouble(ACCOUNT_BALANCE);
      double eq  = AccountInfoDouble(ACCOUNT_EQUITY);
      double dd  = g_RiskManager.GetTotalDDPercent();

      g_Logger.Info(StringFormat("HEARTBEAT | Bal: $%.2f | Eq: $%.2f | DD: %.2f%% | Trades today: %d | News: %s",
                    bal, eq, dd, g_RiskManager.GetTradesToday(), g_NewsFilter.GetStatusString()));
      g_heartbeatCount = 0;
   }
}

//+------------------------------------------------------------------+
//| Trade event handler                                               |
//+------------------------------------------------------------------+
void OnTrade()
{
   // Check for closed trades and update risk manager
   double pnl;
   ulong ticket;

   if(g_TradeManager.GetLastClosedPnL(pnl, ticket))
   {
      // Avoid processing same deal twice
      if(ticket != g_lastDealTicket)
      {
         g_lastDealTicket = ticket;
         g_RiskManager.OnTradeClosed(pnl);

         string typeStr = (pnl >= 0) ? "WIN" : "LOSS";
         g_Logger.LogTradeClose(ticket, "TrendPullback", typeStr,
                               g_TradeManager.GetEntryPrice(),
                               0, 0, pnl);

         g_Logger.Info(StringFormat("TRADE CLOSED | %s | PnL: $%.2f | Consec losses: %d | Day PnL: $%.2f",
                       typeStr, pnl, g_RiskManager.GetConsecLosses(), g_RiskManager.GetDailyPnL()));
      }
   }
}

//+------------------------------------------------------------------+
//| Expert deinitialization                                           |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();

   g_Logger.Info("========== EA DEINIT ==========");
   g_Logger.Info("Reason: " + IntegerToString(reason));
   g_Logger.Info("Final Balance: $" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2));
   g_Logger.Info("Final DD: " + DoubleToString(g_RiskManager.GetTotalDDPercent(), 2) + "%");
   g_Logger.Info("Trades today: " + IntegerToString(g_RiskManager.GetTradesToday()));

   g_Logger.WriteDailySummary();
   g_Logger.Flush();
}

//+------------------------------------------------------------------+
//| Check for new bar on chart timeframe                              |
//+------------------------------------------------------------------+
bool IsNewBar()
{
   datetime currentBarTime = iTime(_Symbol, PERIOD_H1, 0);
   if(currentBarTime == g_lastBarTime) return false;
   g_lastBarTime = currentBarTime;
   return true;
}
//+------------------------------------------------------------------+
