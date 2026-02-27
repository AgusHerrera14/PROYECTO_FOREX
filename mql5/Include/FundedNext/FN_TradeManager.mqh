//+------------------------------------------------------------------+
//|                    FN_TradeManager.mqh                            |
//|        FundedNext EURUSD EA - Trade Execution & Management       |
//|        Handles: execution, breakeven, trailing, partial close    |
//+------------------------------------------------------------------+
#ifndef FN_TRADE_MANAGER_MQH
#define FN_TRADE_MANAGER_MQH

#include <Trade/Trade.mqh>
#include "FN_Config.mqh"
#include "FN_Utils.mqh"

class CTradeManager
{
private:
   CTrade   m_trade;
   int      m_magic;

   // Open position tracking
   ulong    m_openTicket;
   double   m_entryPrice;
   double   m_initialSL;
   double   m_riskPips;
   string   m_openStrategy;
   bool     m_partialClosed;
   bool     m_beActivated;

   // Trade management params
   bool     m_enableBE;
   double   m_beR;
   bool     m_enableTrail;
   double   m_trailStartR;
   double   m_trailATRMult;
   bool     m_enablePartial;
   double   m_partialR;
   double   m_partialPct;

public:
   //+------------------------------------------------------------------+
   CTradeManager() : m_openTicket(0), m_entryPrice(0), m_initialSL(0),
                     m_riskPips(0), m_partialClosed(false), m_beActivated(false) {}

   //+------------------------------------------------------------------+
   void Init(int magic, int maxSlippage,
             bool enableBE, double beR,
             bool enableTrail, double trailStartR, double trailATR,
             bool enablePartial, double partialR, double partialPct)
   {
      m_magic          = magic;
      m_enableBE       = enableBE;
      m_beR            = beR;
      m_enableTrail    = enableTrail;
      m_trailStartR    = trailStartR;
      m_trailATRMult   = trailATR;
      m_enablePartial  = enablePartial;
      m_partialR       = partialR;
      m_partialPct     = partialPct;

      m_trade.SetExpertMagicNumber(magic);
      m_trade.SetDeviationInPoints(maxSlippage);
      m_trade.SetTypeFilling(ORDER_FILLING_IOC);  // Most compatible

      Print("[FN-TRADE] Initialized. Magic: ", magic,
            " | BE: ", enableBE, " | Trail: ", enableTrail);
   }

   //+------------------------------------------------------------------+
   //| Execute a signal                                                  |
   //+------------------------------------------------------------------+
   bool ExecuteSignal(SSignal &signal, double lots, string strategy)
   {
      if(lots <= 0 || signal.direction == 0) return false;

      string comment = "FN_" + strategy;
      bool result = false;
      int retries = 3;

      while(retries > 0)
      {
         if(signal.direction > 0)
         {
            double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
            result = m_trade.Buy(lots, _Symbol, ask, signal.sl, signal.tp, comment);
         }
         else
         {
            double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
            result = m_trade.Sell(lots, _Symbol, bid, signal.sl, signal.tp, comment);
         }

         if(result && m_trade.ResultRetcode() == TRADE_RETCODE_DONE)
         {
            m_openTicket    = m_trade.ResultOrder();
            m_entryPrice    = m_trade.ResultPrice();
            m_initialSL     = signal.sl;
            m_riskPips      = signal.slPips;
            m_openStrategy  = strategy;
            m_partialClosed = false;
            m_beActivated   = false;

            Print("[FN-TRADE] OPENED: ", (signal.direction > 0 ? "BUY" : "SELL"),
                  " | Ticket: ", m_openTicket,
                  " | Price: ", DoubleToString(m_entryPrice, 5),
                  " | SL: ", DoubleToString(signal.sl, 5),
                  " | TP: ", DoubleToString(signal.tp, 5),
                  " | Lots: ", DoubleToString(lots, 2));
            return true;
         }

         retries--;
         if(retries > 0) Sleep(500);
      }

      Print("[FN-TRADE] EXECUTION FAILED after 3 retries. Error: ",
            m_trade.ResultRetcode(), " ", m_trade.ResultRetcodeDescription());
      return false;
   }

   //+------------------------------------------------------------------+
   //| Manage open positions: BE, trailing, partial close               |
   //| Call this on every tick                                           |
   //+------------------------------------------------------------------+
   void ManagePositions(double currentATR)
   {
      if(!HasOurPosition()) return;

      for(int i = PositionsTotal() - 1; i >= 0; i--)
      {
         ulong ticket = PositionGetTicket(i);
         if(ticket == 0) continue;
         if(PositionGetInteger(POSITION_MAGIC) != m_magic) continue;
         if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;

         double openPrice  = PositionGetDouble(POSITION_PRICE_OPEN);
         double currentSL  = PositionGetDouble(POSITION_SL);
         double currentTP  = PositionGetDouble(POSITION_TP);
         double volume     = PositionGetDouble(POSITION_VOLUME);
         ENUM_POSITION_TYPE posType = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);

         double currentPrice;
         if(posType == POSITION_TYPE_BUY)
            currentPrice = SymbolInfoDouble(_Symbol, SYMBOL_BID);
         else
            currentPrice = SymbolInfoDouble(_Symbol, SYMBOL_ASK);

         double profitPips = 0;
         if(posType == POSITION_TYPE_BUY)
            profitPips = FN_PriceToPips(currentPrice - openPrice);
         else
            profitPips = FN_PriceToPips(openPrice - currentPrice);

         double rMultiple = (m_riskPips > 0) ? profitPips / m_riskPips : 0;

         //--- 1. BREAKEVEN ---
         if(m_enableBE && !m_beActivated && rMultiple >= m_beR)
         {
            double newSL;
            if(posType == POSITION_TYPE_BUY)
               newSL = openPrice + FN_PipSize();  // 1 pip above entry
            else
               newSL = openPrice - FN_PipSize();  // 1 pip below entry

            // Only move if it's an improvement
            if((posType == POSITION_TYPE_BUY && newSL > currentSL) ||
               (posType == POSITION_TYPE_SELL && newSL < currentSL))
            {
               if(m_trade.PositionModify(ticket, NormalizeDouble(newSL, _Digits), currentTP))
               {
                  m_beActivated = true;
                  Print("[FN-TRADE] BREAKEVEN activated at ", DoubleToString(rMultiple, 1), "R");
               }
            }
         }

         //--- 2. PARTIAL CLOSE ---
         if(m_enablePartial && !m_partialClosed && rMultiple >= m_partialR)
         {
            double closeVol = NormalizeDouble(volume * m_partialPct / 100.0, 2);
            double minVol = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);

            if(closeVol >= minVol && (volume - closeVol) >= minVol)
            {
               if(m_trade.PositionClosePartial(ticket, closeVol))
               {
                  m_partialClosed = true;
                  Print("[FN-TRADE] PARTIAL CLOSE: ", DoubleToString(closeVol, 2),
                        " lots at ", DoubleToString(rMultiple, 1), "R");
               }
            }
         }

         //--- 3. TRAILING STOP ---
         if(m_enableTrail && m_beActivated && rMultiple >= m_trailStartR && currentATR > 0)
         {
            double trailDist = currentATR * m_trailATRMult;
            double newSL;

            if(posType == POSITION_TYPE_BUY)
            {
               newSL = currentPrice - trailDist;
               if(newSL > currentSL && newSL > openPrice)
               {
                  m_trade.PositionModify(ticket, NormalizeDouble(newSL, _Digits), currentTP);
               }
            }
            else
            {
               newSL = currentPrice + trailDist;
               if(newSL < currentSL && newSL < openPrice)
               {
                  m_trade.PositionModify(ticket, NormalizeDouble(newSL, _Digits), currentTP);
               }
            }
         }
      }
   }

   //+------------------------------------------------------------------+
   //| Check if we have an open position with our magic                 |
   //+------------------------------------------------------------------+
   bool HasOurPosition()
   {
      for(int i = PositionsTotal() - 1; i >= 0; i--)
      {
         ulong ticket = PositionGetTicket(i);
         if(ticket == 0) continue;
         if(PositionGetInteger(POSITION_MAGIC) == m_magic &&
            PositionGetString(POSITION_SYMBOL) == _Symbol)
            return true;
      }
      return false;
   }

   //+------------------------------------------------------------------+
   //| Close all positions with our magic                                |
   //+------------------------------------------------------------------+
   void CloseAllPositions(string reason)
   {
      for(int i = PositionsTotal() - 1; i >= 0; i--)
      {
         ulong ticket = PositionGetTicket(i);
         if(ticket == 0) continue;
         if(PositionGetInteger(POSITION_MAGIC) != m_magic) continue;
         if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;

         m_trade.PositionClose(ticket);
         Print("[FN-TRADE] CLOSED position ", ticket, " | Reason: ", reason);
      }
   }

   //+------------------------------------------------------------------+
   //| Cancel all pending orders with our magic                          |
   //+------------------------------------------------------------------+
   void CancelAllPending(string reason)
   {
      for(int i = OrdersTotal() - 1; i >= 0; i--)
      {
         ulong ticket = OrderGetTicket(i);
         if(ticket == 0) continue;
         if(OrderGetInteger(ORDER_MAGIC) != m_magic) continue;
         if(OrderGetString(ORDER_SYMBOL) != _Symbol) continue;

         m_trade.OrderDelete(ticket);
         Print("[FN-TRADE] CANCELLED pending order ", ticket, " | Reason: ", reason);
      }
   }

   //+------------------------------------------------------------------+
   //| Move open position SL to breakeven (for news protection)         |
   //+------------------------------------------------------------------+
   void ForceBreakeven()
   {
      if(m_beActivated) return;  // Already at BE

      for(int i = PositionsTotal() - 1; i >= 0; i--)
      {
         ulong ticket = PositionGetTicket(i);
         if(ticket == 0) continue;
         if(PositionGetInteger(POSITION_MAGIC) != m_magic) continue;
         if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;

         double openPrice = PositionGetDouble(POSITION_PRICE_OPEN);
         double currentSL = PositionGetDouble(POSITION_SL);
         double currentTP = PositionGetDouble(POSITION_TP);
         ENUM_POSITION_TYPE posType = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);

         // Only move to BE if currently in profit
         double currentPrice;
         if(posType == POSITION_TYPE_BUY)
            currentPrice = SymbolInfoDouble(_Symbol, SYMBOL_BID);
         else
            currentPrice = SymbolInfoDouble(_Symbol, SYMBOL_ASK);

         bool inProfit = (posType == POSITION_TYPE_BUY) ?
                         (currentPrice > openPrice) : (currentPrice < openPrice);

         if(inProfit)
         {
            double newSL = openPrice;
            if((posType == POSITION_TYPE_BUY && newSL > currentSL) ||
               (posType == POSITION_TYPE_SELL && newSL < currentSL))
            {
               m_trade.PositionModify(ticket, NormalizeDouble(newSL, _Digits), currentTP);
               m_beActivated = true;
               Print("[FN-TRADE] NEWS: Forced breakeven on ticket ", ticket);
            }
         }
      }
   }

   //+------------------------------------------------------------------+
   //| Get last closed trade P&L (call from OnTrade)                    |
   //+------------------------------------------------------------------+
   bool GetLastClosedPnL(double &pnl, ulong &ticket)
   {
      if(!HistorySelect(TimeCurrent() - 86400, TimeCurrent()))
         return false;

      int total = HistoryDealsTotal();
      for(int i = total - 1; i >= 0; i--)
      {
         ulong dealTicket = HistoryDealGetTicket(i);
         if(dealTicket == 0) continue;
         if(HistoryDealGetInteger(dealTicket, DEAL_MAGIC) != m_magic) continue;
         if(HistoryDealGetString(dealTicket, DEAL_SYMBOL) != _Symbol) continue;

         ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(dealTicket, DEAL_ENTRY);
         if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_OUT_BY)
         {
            pnl = HistoryDealGetDouble(dealTicket, DEAL_PROFIT) +
                  HistoryDealGetDouble(dealTicket, DEAL_SWAP) +
                  HistoryDealGetDouble(dealTicket, DEAL_COMMISSION);
            ticket = dealTicket;
            return true;
         }
      }
      return false;
   }

   //+------------------------------------------------------------------+
   ulong  GetOpenTicket()  { return m_openTicket; }
   string GetOpenStrategy(){ return m_openStrategy; }
   double GetEntryPrice()  { return m_entryPrice; }
   double GetRiskPips()    { return m_riskPips; }
};

#endif
