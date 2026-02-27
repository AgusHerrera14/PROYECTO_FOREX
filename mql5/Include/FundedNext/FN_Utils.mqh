//+------------------------------------------------------------------+
//|                         FN_Utils.mqh                              |
//|        FundedNext EURUSD EA - Utility Functions                   |
//+------------------------------------------------------------------+
#ifndef FN_UTILS_MQH
#define FN_UTILS_MQH

//+------------------------------------------------------------------+
//| Pip size for the current symbol                                   |
//+------------------------------------------------------------------+
double FN_PipSize()
{
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   if(digits == 3 || digits == 5)
      return _Point * 10.0;
   return _Point;
}

//+------------------------------------------------------------------+
//| Pip value in account currency for given lot size                  |
//+------------------------------------------------------------------+
double FN_PipValue(double lots)
{
   double tickValue = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tickSize <= 0) return 0;
   return (FN_PipSize() / tickSize) * tickValue * lots;
}

//+------------------------------------------------------------------+
//| Convert price distance to pips                                    |
//+------------------------------------------------------------------+
double FN_PriceToPips(double priceDistance)
{
   double pipSz = FN_PipSize();
   if(pipSz <= 0) return 0;
   return MathAbs(priceDistance) / pipSz;
}

//+------------------------------------------------------------------+
//| Convert pips to price distance                                    |
//+------------------------------------------------------------------+
double FN_PipsToPrice(double pips)
{
   return pips * FN_PipSize();
}

//+------------------------------------------------------------------+
//| Normalize lot size to broker requirements                        |
//+------------------------------------------------------------------+
double FN_NormalizeLots(double lots)
{
   double minLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double stepLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);

   if(stepLot <= 0) stepLot = 0.01;

   lots = MathFloor(lots / stepLot) * stepLot;
   lots = MathMax(minLot, MathMin(maxLot, lots));

   return NormalizeDouble(lots, 2);
}

//+------------------------------------------------------------------+
//| Current spread in pips                                            |
//+------------------------------------------------------------------+
double FN_SpreadPips()
{
   double ask   = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid   = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double pipSz = FN_PipSize();
   if(pipSz <= 0) return 999.0;
   return (ask - bid) / pipSz;
}

//+------------------------------------------------------------------+
//| Check if spread is acceptable                                     |
//+------------------------------------------------------------------+
bool FN_IsSpreadOK(double maxPips)
{
   return FN_SpreadPips() <= maxPips;
}

//+------------------------------------------------------------------+
//| Current UTC hour                                                  |
//+------------------------------------------------------------------+
int FN_UTCHour()
{
   MqlDateTime dt;
   TimeToStruct(TimeGMT(), dt);
   return dt.hour;
}

//+------------------------------------------------------------------+
//| Check if running in Strategy Tester                               |
//+------------------------------------------------------------------+
bool FN_IsTester()
{
   return (bool)MQLInfoInteger(MQL_TESTER);
}

//+------------------------------------------------------------------+
//| Get ATR value from handle                                         |
//+------------------------------------------------------------------+
double FN_GetATR(int atrHandle, int shift = 1)
{
   double buf[];
   ArraySetAsSeries(buf, true);
   if(CopyBuffer(atrHandle, 0, shift, 1, buf) <= 0)
      return 0;
   return buf[0];
}

//+------------------------------------------------------------------+
//| Get indicator value from handle (single buffer)                   |
//+------------------------------------------------------------------+
double FN_GetIndicator(int handle, int bufferIndex, int shift)
{
   double buf[];
   ArraySetAsSeries(buf, true);
   if(CopyBuffer(handle, bufferIndex, shift, 1, buf) <= 0)
      return 0;
   return buf[0];
}

//+------------------------------------------------------------------+
//| Get indicator array from handle                                   |
//+------------------------------------------------------------------+
bool FN_GetIndicatorArray(int handle, int bufferIndex, int shift, int count, double &arr[])
{
   ArraySetAsSeries(arr, true);
   return CopyBuffer(handle, bufferIndex, shift, count, arr) == count;
}

#endif
