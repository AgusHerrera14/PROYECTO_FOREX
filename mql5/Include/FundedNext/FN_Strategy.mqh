//+------------------------------------------------------------------+
//|                      FN_Strategy.mqh                              |
//|        FundedNext EURUSD EA - EMA Trend Pullback Strategy        |
//|                                                                   |
//| STRATEGY: Multi-timeframe EMA Trend + RSI Pullback               |
//|   Trend:  H4 EMA200 slope + EMA50 > EMA200 + ADX > threshold    |
//|   Entry:  H1 RSI pullback to 38-52 zone + price at EMA21        |
//|   SL:     ATR-based below swing                                   |
//|   TP:     RR ratio x SL distance                                 |
//|                                                                   |
//| Parameters: 5 core (EMA fast, slow, trend, RSI period, RR)      |
//| Edge: Trend-following pullback with multi-TF confirmation        |
//+------------------------------------------------------------------+
#ifndef FN_STRATEGY_MQH
#define FN_STRATEGY_MQH

#include "FN_Config.mqh"
#include "FN_Utils.mqh"

class CStrategy
{
private:
   // H1 indicator handles
   int   m_h_ema21_h1;
   int   m_h_rsi_h1;
   int   m_h_atr_h1;

   // H4 indicator handles
   int   m_h_ema50_h4;
   int   m_h_ema200_h4;
   int   m_h_adx_h4;

   // Parameters (cached from inputs)
   int    m_emaFast;
   int    m_emaSlow;
   int    m_emaTrend;
   int    m_rsiPeriod;
   int    m_atrPeriod;
   int    m_adxPeriod;
   int    m_adxThreshold;
   double m_rrRatio;
   double m_rsiBuyLow, m_rsiBuyHigh;
   double m_rsiSellLow, m_rsiSellHigh;

   bool   m_initialized;

public:
   //+------------------------------------------------------------------+
   CStrategy() : m_initialized(false),
      m_h_ema21_h1(INVALID_HANDLE), m_h_rsi_h1(INVALID_HANDLE),
      m_h_atr_h1(INVALID_HANDLE), m_h_ema50_h4(INVALID_HANDLE),
      m_h_ema200_h4(INVALID_HANDLE), m_h_adx_h4(INVALID_HANDLE) {}

   //+------------------------------------------------------------------+
   ~CStrategy() { ReleaseHandles(); }

   //+------------------------------------------------------------------+
   bool Init(string symbol, int emaFast, int emaSlow, int emaTrend,
             int rsiPeriod, int atrPeriod, int adxPeriod, int adxThresh,
             double rrRatio,
             double rsiBuyLo, double rsiBuyHi,
             double rsiSellLo, double rsiSellHi)
   {
      m_emaFast      = emaFast;
      m_emaSlow      = emaSlow;
      m_emaTrend     = emaTrend;
      m_rsiPeriod    = rsiPeriod;
      m_atrPeriod    = atrPeriod;
      m_adxPeriod    = adxPeriod;
      m_adxThreshold = adxThresh;
      m_rrRatio      = rrRatio;
      m_rsiBuyLow    = rsiBuyLo;
      m_rsiBuyHigh   = rsiBuyHi;
      m_rsiSellLow   = rsiSellLo;
      m_rsiSellHigh  = rsiSellHi;

      // Create H1 indicators
      m_h_ema21_h1  = iMA(symbol, PERIOD_H1, emaFast, 0, MODE_EMA, PRICE_CLOSE);
      m_h_rsi_h1    = iRSI(symbol, PERIOD_H1, rsiPeriod, PRICE_CLOSE);
      m_h_atr_h1    = iATR(symbol, PERIOD_H1, atrPeriod);

      // Create H4 indicators
      m_h_ema50_h4  = iMA(symbol, PERIOD_H4, emaSlow, 0, MODE_EMA, PRICE_CLOSE);
      m_h_ema200_h4 = iMA(symbol, PERIOD_H4, emaTrend, 0, MODE_EMA, PRICE_CLOSE);
      m_h_adx_h4    = iADX(symbol, PERIOD_H4, adxPeriod);

      // Verify all handles
      if(m_h_ema21_h1  == INVALID_HANDLE || m_h_rsi_h1  == INVALID_HANDLE ||
         m_h_atr_h1    == INVALID_HANDLE || m_h_ema50_h4 == INVALID_HANDLE ||
         m_h_ema200_h4 == INVALID_HANDLE || m_h_adx_h4   == INVALID_HANDLE)
      {
         Print("[FN-STRAT] ERROR: Failed to create indicator handles");
         ReleaseHandles();
         return false;
      }

      m_initialized = true;
      Print("[FN-STRAT] Initialized: EMA Trend Pullback (H4 trend, H1 entry)");
      return true;
   }

   //+------------------------------------------------------------------+
   //| Generate trading signal                                           |
   //| Call only at new H1 bar close (shift=1 is the completed bar)    |
   //+------------------------------------------------------------------+
   bool GetSignal(SSignal &signal)
   {
      signal.Reset();
      if(!m_initialized) return false;

      //--- Get H4 trend data ---
      double ema50_h4  = FN_GetIndicator(m_h_ema50_h4, 0, 1);
      double ema200_h4 = FN_GetIndicator(m_h_ema200_h4, 0, 1);

      // ADX main line (buffer 0), +DI (buffer 1), -DI (buffer 2)
      double adx_h4    = FN_GetIndicator(m_h_adx_h4, 0, 1);
      double plusDI_h4  = FN_GetIndicator(m_h_adx_h4, 1, 1);
      double minusDI_h4 = FN_GetIndicator(m_h_adx_h4, 2, 1);

      // H4 close price
      double h4Close[];
      ArraySetAsSeries(h4Close, true);
      if(CopyClose(_Symbol, PERIOD_H4, 1, 1, h4Close) <= 0) return false;

      if(ema50_h4 == 0 || ema200_h4 == 0 || adx_h4 == 0) return false;

      // Determine H4 trend
      bool uptrend  = (h4Close[0] > ema200_h4) && (ema50_h4 > ema200_h4) &&
                      (adx_h4 > m_adxThreshold) && (plusDI_h4 > minusDI_h4);
      bool downtrend = (h4Close[0] < ema200_h4) && (ema50_h4 < ema200_h4) &&
                       (adx_h4 > m_adxThreshold) && (minusDI_h4 > plusDI_h4);

      if(!uptrend && !downtrend)
         return false;  // No clear H4 trend

      //--- Get H1 entry data (completed bar = shift 1) ---
      double ema21_h1 = FN_GetIndicator(m_h_ema21_h1, 0, 1);
      double rsi_h1   = FN_GetIndicator(m_h_rsi_h1, 0, 1);
      double atr_h1   = FN_GetIndicator(m_h_atr_h1, 0, 1);

      if(ema21_h1 == 0 || rsi_h1 == 0 || atr_h1 == 0) return false;

      // H1 OHLC data
      double h1Open[], h1High[], h1Low[], h1Close[];
      ArraySetAsSeries(h1Open, true);
      ArraySetAsSeries(h1High, true);
      ArraySetAsSeries(h1Low, true);
      ArraySetAsSeries(h1Close, true);

      if(CopyOpen(_Symbol, PERIOD_H1, 1, 3, h1Open)   <= 0) return false;
      if(CopyHigh(_Symbol, PERIOD_H1, 1, 3, h1High)   <= 0) return false;
      if(CopyLow(_Symbol, PERIOD_H1, 1, 3, h1Low)     <= 0) return false;
      if(CopyClose(_Symbol, PERIOD_H1, 1, 3, h1Close) <= 0) return false;

      // bar[0] = last completed, bar[1] = one before, bar[2] = two before
      double close0 = h1Close[0];
      double open0  = h1Open[0];
      double low0   = h1Low[0];
      double high0  = h1High[0];
      double low1   = h1Low[1];
      double low2   = h1Low[2];
      double high1  = h1High[1];
      double high2  = h1High[2];

      // EMA zone tolerance (within 0.2% of EMA21)
      double emaTolerance = ema21_h1 * 0.002;

      //=== BUY SIGNAL (uptrend pullback) ===
      if(uptrend)
      {
         // Price pulled back to EMA21 zone
         bool nearEMA = (low0 <= ema21_h1 + emaTolerance) && (close0 > ema21_h1);

         // RSI in pullback zone (not extreme)
         bool rsiOK = (rsi_h1 >= m_rsiBuyLow && rsi_h1 <= m_rsiBuyHigh);

         // Bullish candle
         bool bullish = (close0 > open0);

         if(nearEMA && rsiOK && bullish)
         {
            // SL below recent swing low (min of last 3 lows) minus ATR buffer
            double swingLow = MathMin(low0, MathMin(low1, low2));
            double sl = swingLow - atr_h1 * 0.5;

            // Ensure SL is below EMA21
            sl = MathMin(sl, ema21_h1 - atr_h1 * 0.3);

            double entry = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
            double slDist = entry - sl;

            // Sanity: SL distance between 0.5 and 3.0 ATR
            if(slDist >= atr_h1 * 0.3 && slDist <= atr_h1 * 3.0)
            {
               double tp = entry + slDist * m_rrRatio;

               signal.direction = 1;  // BUY
               signal.entry     = entry;
               signal.sl        = NormalizeDouble(sl, _Digits);
               signal.tp        = NormalizeDouble(tp, _Digits);
               signal.slPips    = FN_PriceToPips(slDist);
               signal.tpPips    = FN_PriceToPips(tp - entry);
               signal.reason    = StringFormat("BUY_PULLBACK|RSI=%.1f|ATR=%.5f|ADX=%.1f",
                                               rsi_h1, atr_h1, adx_h4);
               return true;
            }
         }
      }

      //=== SELL SIGNAL (downtrend pullback) ===
      if(downtrend)
      {
         // Price pulled back to EMA21 zone from below
         bool nearEMA = (high0 >= ema21_h1 - emaTolerance) && (close0 < ema21_h1);

         // RSI in sell pullback zone
         bool rsiOK = (rsi_h1 >= m_rsiSellLow && rsi_h1 <= m_rsiSellHigh);

         // Bearish candle
         bool bearish = (close0 < open0);

         if(nearEMA && rsiOK && bearish)
         {
            // SL above recent swing high + ATR buffer
            double swingHigh = MathMax(high0, MathMax(high1, high2));
            double sl = swingHigh + atr_h1 * 0.5;

            // Ensure SL is above EMA21
            sl = MathMax(sl, ema21_h1 + atr_h1 * 0.3);

            double entry = SymbolInfoDouble(_Symbol, SYMBOL_BID);
            double slDist = sl - entry;

            // Sanity check
            if(slDist >= atr_h1 * 0.3 && slDist <= atr_h1 * 3.0)
            {
               double tp = entry - slDist * m_rrRatio;

               signal.direction = -1;  // SELL
               signal.entry     = entry;
               signal.sl        = NormalizeDouble(sl, _Digits);
               signal.tp        = NormalizeDouble(tp, _Digits);
               signal.slPips    = FN_PriceToPips(slDist);
               signal.tpPips    = FN_PriceToPips(entry - tp);
               signal.reason    = StringFormat("SELL_PULLBACK|RSI=%.1f|ATR=%.5f|ADX=%.1f",
                                               rsi_h1, atr_h1, adx_h4);
               return true;
            }
         }
      }

      return false;
   }

   //+------------------------------------------------------------------+
   //| Get current ATR value (for trade management)                     |
   //+------------------------------------------------------------------+
   double GetATR()
   {
      if(!m_initialized) return 0;
      return FN_GetATR(m_h_atr_h1, 1);
   }

private:
   //+------------------------------------------------------------------+
   void ReleaseHandles()
   {
      if(m_h_ema21_h1  != INVALID_HANDLE) IndicatorRelease(m_h_ema21_h1);
      if(m_h_rsi_h1    != INVALID_HANDLE) IndicatorRelease(m_h_rsi_h1);
      if(m_h_atr_h1    != INVALID_HANDLE) IndicatorRelease(m_h_atr_h1);
      if(m_h_ema50_h4  != INVALID_HANDLE) IndicatorRelease(m_h_ema50_h4);
      if(m_h_ema200_h4 != INVALID_HANDLE) IndicatorRelease(m_h_ema200_h4);
      if(m_h_adx_h4    != INVALID_HANDLE) IndicatorRelease(m_h_adx_h4);
   }
};

#endif
