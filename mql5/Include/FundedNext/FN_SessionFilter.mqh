//+------------------------------------------------------------------+
//|                     FN_SessionFilter.mqh                          |
//|        FundedNext EURUSD EA - Trading Session Filter              |
//+------------------------------------------------------------------+
#ifndef FN_SESSION_FILTER_MQH
#define FN_SESSION_FILTER_MQH

#include "FN_Utils.mqh"

//+------------------------------------------------------------------+
//| Check if current time is within allowed trading session           |
//| Uses UTC time. Default: 07:00-17:00 (London open to NY close)   |
//+------------------------------------------------------------------+
bool FN_IsActiveSession(bool filterEnabled, int startHour, int endHour)
{
   if(!filterEnabled) return true;

   int utcHour = FN_UTCHour();

   // Handle wrap-around (e.g., start=22, end=6 for Asian session)
   if(startHour <= endHour)
      return (utcHour >= startHour && utcHour < endHour);
   else
      return (utcHour >= startHour || utcHour < endHour);
}

//+------------------------------------------------------------------+
//| Check if we're in London session (07:00-10:00 UTC)               |
//+------------------------------------------------------------------+
bool FN_IsLondonOpen()
{
   int h = FN_UTCHour();
   return (h >= 7 && h < 10);
}

//+------------------------------------------------------------------+
//| Check if we're in NY session (13:00-16:00 UTC)                   |
//+------------------------------------------------------------------+
bool FN_IsNYOpen()
{
   int h = FN_UTCHour();
   return (h >= 13 && h < 16);
}

//+------------------------------------------------------------------+
//| Check if it's a high-liquidity period (London or NY open)        |
//+------------------------------------------------------------------+
bool FN_IsHighLiquidity()
{
   return FN_IsLondonOpen() || FN_IsNYOpen();
}

#endif
