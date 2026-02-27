//+------------------------------------------------------------------+
//|                      FN_NewsFilter.mqh                            |
//|        FundedNext EURUSD EA - News Filter (Economic Calendar)    |
//|                                                                   |
//| Priority A: MQL5 built-in CalendarValueHistory                   |
//| Fallback:   Disable trading if calendar unavailable              |
//| In Tester:  News filter disabled (calendar not available)        |
//+------------------------------------------------------------------+
#ifndef FN_NEWS_FILTER_MQH
#define FN_NEWS_FILTER_MQH

#include "FN_Utils.mqh"

class CNewsFilter
{
private:
   bool     m_enabled;
   int      m_preMinutes;
   int      m_postMinutes;
   bool     m_cancelPendings;
   bool     m_moveToBE;
   bool     m_isTester;

   // Cache
   datetime m_nextEventTime;
   string   m_nextEventName;
   datetime m_lastCacheRefresh;
   bool     m_calendarAvailable;
   int      m_cacheTTL;          // seconds

public:
   //+------------------------------------------------------------------+
   CNewsFilter() : m_enabled(false), m_preMinutes(8), m_postMinutes(8),
                   m_cancelPendings(true), m_moveToBE(true), m_isTester(false),
                   m_nextEventTime(0), m_nextEventName(""),
                   m_lastCacheRefresh(0), m_calendarAvailable(false),
                   m_cacheTTL(3600) {}

   //+------------------------------------------------------------------+
   void Init(bool enabled, int preMin, int postMin, bool cancelPend, bool moveBE)
   {
      m_enabled        = enabled;
      m_preMinutes     = preMin;
      m_postMinutes    = postMin;
      m_cancelPendings = cancelPend;
      m_moveToBE       = moveBE;
      m_isTester       = FN_IsTester();

      if(m_isTester)
      {
         Print("[FN-NEWS] Running in Tester - news filter DISABLED");
         m_calendarAvailable = false;
         return;
      }

      if(!m_enabled)
      {
         Print("[FN-NEWS] News filter DISABLED by config");
         return;
      }

      // Test calendar availability
      RefreshCache();
   }

   //+------------------------------------------------------------------+
   //| Refresh the cached next high-impact event                       |
   //+------------------------------------------------------------------+
   void RefreshCache()
   {
      if(!m_enabled || m_isTester) return;

      datetime now = TimeCurrent();

      // Don't refresh too often
      if(m_lastCacheRefresh > 0 && (now - m_lastCacheRefresh) < m_cacheTTL)
         return;

      m_lastCacheRefresh = now;
      m_calendarAvailable = false;
      m_nextEventTime = 0;
      m_nextEventName = "";

      // Search for events in the next 24 hours
      datetime fromTime = now - 3600;      // 1 hour ago (catch current windows)
      datetime toTime   = now + 86400;     // 24 hours ahead

      MqlCalendarValue values[];

      // Get EUR events
      int eurCount = CalendarValueHistory(values, fromTime, toTime, NULL, "EUR");

      // Get USD events
      MqlCalendarValue usdValues[];
      int usdCount = CalendarValueHistory(usdValues, fromTime, toTime, NULL, "USD");

      if(eurCount < 0 && usdCount < 0)
      {
         Print("[FN-NEWS] Calendar API unavailable - FAIL SAFE active");
         m_calendarAvailable = false;
         return;
      }

      m_calendarAvailable = true;

      // Find the nearest HIGH impact event from now
      datetime nearest = now + 86400 * 7;  // Far future default
      string nearestName = "";

      // Process EUR events
      for(int i = 0; i < ArraySize(values); i++)
      {
         MqlCalendarEvent event;
         if(!CalendarEventById(values[i].event_id, event))
            continue;

         if(event.importance != CALENDAR_IMPORTANCE_HIGH)
            continue;

         if(values[i].time >= (now - m_postMinutes * 60) && values[i].time < nearest)
         {
            nearest = values[i].time;
            nearestName = event.name;
         }
      }

      // Process USD events
      for(int i = 0; i < ArraySize(usdValues); i++)
      {
         MqlCalendarEvent event;
         if(!CalendarEventById(usdValues[i].event_id, event))
            continue;

         if(event.importance != CALENDAR_IMPORTANCE_HIGH)
            continue;

         if(usdValues[i].time >= (now - m_postMinutes * 60) && usdValues[i].time < nearest)
         {
            nearest = usdValues[i].time;
            nearestName = event.name;
         }
      }

      m_nextEventTime = nearest;
      m_nextEventName = nearestName;

      if(nearest < now + 86400)
         Print("[FN-NEWS] Next high-impact: ", nearestName,
               " at ", TimeToString(nearest, TIME_DATE|TIME_MINUTES));
   }

   //+------------------------------------------------------------------+
   //| Is current time within a news window?                            |
   //+------------------------------------------------------------------+
   bool IsNewsWindow()
   {
      if(!m_enabled) return false;
      if(m_isTester) return false;

      // FAIL-SAFE: if calendar is not available, BLOCK trading
      if(!m_calendarAvailable)
         return true;  // Block = treat as if news is happening

      datetime now = TimeCurrent();

      if(m_nextEventTime == 0) return false;

      datetime windowStart = m_nextEventTime - m_preMinutes * 60;
      datetime windowEnd   = m_nextEventTime + m_postMinutes * 60;

      return (now >= windowStart && now <= windowEnd);
   }

   //+------------------------------------------------------------------+
   //| Should we cancel pending orders? (before news)                   |
   //+------------------------------------------------------------------+
   bool ShouldCancelPendings()
   {
      if(!m_cancelPendings) return false;
      return IsNewsWindow();
   }

   //+------------------------------------------------------------------+
   //| Should we move open positions to breakeven?                      |
   //+------------------------------------------------------------------+
   bool ShouldMoveToBreakeven()
   {
      if(!m_moveToBE) return false;
      return IsNewsWindow();
   }

   //+------------------------------------------------------------------+
   //| Status string for logging                                        |
   //+------------------------------------------------------------------+
   string GetStatusString()
   {
      if(!m_enabled)          return "DISABLED";
      if(m_isTester)          return "TESTER_OFF";
      if(!m_calendarAvailable) return "FAILSAFE_BLOCK";
      if(IsNewsWindow())      return "BLOCKED_" + m_nextEventName;
      return "CLEAR";
   }

   //+------------------------------------------------------------------+
   //| Minutes until next event (for display/logging)                   |
   //+------------------------------------------------------------------+
   int MinutesToNextEvent()
   {
      if(m_nextEventTime == 0) return 9999;
      datetime now = TimeCurrent();
      if(m_nextEventTime <= now) return 0;
      return (int)((m_nextEventTime - now) / 60);
   }

   //+------------------------------------------------------------------+
   bool IsEnabled()          { return m_enabled; }
   bool IsCalendarAvailable() { return m_calendarAvailable; }
};

#endif
