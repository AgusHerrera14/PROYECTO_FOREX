//+------------------------------------------------------------------+
//|                        FN_Logger.mqh                              |
//|        FundedNext EURUSD EA - Trade & System Logging              |
//+------------------------------------------------------------------+
#ifndef FN_LOGGER_MQH
#define FN_LOGGER_MQH

class CLogger
{
private:
   int      m_tradeFile;
   int      m_systemFile;
   bool     m_enabled;
   bool     m_verbose;
   string   m_tradeFilename;
   string   m_systemFilename;

   // Daily summary tracking
   double   m_dayStartBalance;
   int      m_dayTrades;
   int      m_dayWins;
   double   m_dayPnL;
   double   m_dayMaxDD;
   int      m_dayDate;

public:
   //+------------------------------------------------------------------+
   CLogger() : m_tradeFile(INVALID_HANDLE), m_systemFile(INVALID_HANDLE),
               m_enabled(false), m_verbose(false),
               m_dayStartBalance(0), m_dayTrades(0), m_dayWins(0),
               m_dayPnL(0), m_dayMaxDD(0), m_dayDate(0) {}

   //+------------------------------------------------------------------+
   ~CLogger() { Flush(); }

   //+------------------------------------------------------------------+
   bool Init(bool enabled, bool verbose)
   {
      m_enabled = enabled;
      m_verbose = verbose;
      if(!m_enabled) return true;

      // Monthly file for trades, daily file for system log
      MqlDateTime dt;
      TimeCurrent(dt);
      string monthStr = StringFormat("%04d%02d", dt.year, dt.mon);
      string dayStr   = StringFormat("%04d%02d%02d", dt.year, dt.mon, dt.day);

      m_tradeFilename  = "FN_Trades_" + monthStr + ".csv";
      m_systemFilename = "FN_System_" + dayStr + ".log";

      // Open trade CSV (append mode)
      bool isNew = !FileIsExist(m_tradeFilename);
      m_tradeFile = FileOpen(m_tradeFilename,
                             FILE_READ|FILE_WRITE|FILE_CSV|FILE_SHARE_READ|FILE_ANSI, ',');
      if(m_tradeFile == INVALID_HANDLE)
      {
         Print("[FN-ERROR] Cannot open trade log: ", m_tradeFilename);
         return false;
      }

      if(isNew || FileSize(m_tradeFile) < 10)
      {
         FileWrite(m_tradeFile,
            "Timestamp","Ticket","Strategy","Type","Symbol",
            "EntryPrice","SL","TP","Lots","RiskPct",
            "SpreadPips","Equity","Balance","DD_Pct",
            "NewsFilter","Reason","PnL","ExitPrice");
      }
      FileSeek(m_tradeFile, 0, SEEK_END);

      // Open system log (append mode)
      m_systemFile = FileOpen(m_systemFilename,
                              FILE_READ|FILE_WRITE|FILE_TXT|FILE_SHARE_READ|FILE_ANSI);
      if(m_systemFile != INVALID_HANDLE)
         FileSeek(m_systemFile, 0, SEEK_END);

      // Init daily tracking
      m_dayDate = dt.day_of_year;
      m_dayStartBalance = AccountInfoDouble(ACCOUNT_BALANCE);
      m_dayTrades = 0;
      m_dayWins   = 0;
      m_dayPnL    = 0;
      m_dayMaxDD  = 0;

      return true;
   }

   //+------------------------------------------------------------------+
   //| Log a trade entry                                                 |
   //+------------------------------------------------------------------+
   void LogTradeOpen(ulong ticket, string strategy, string type,
                     double entry, double sl, double tp, double lots,
                     double riskPct, double spreadPips, string newsStatus,
                     string reason)
   {
      if(!m_enabled || m_tradeFile == INVALID_HANDLE) return;

      double equity  = AccountInfoDouble(ACCOUNT_EQUITY);
      double balance = AccountInfoDouble(ACCOUNT_BALANCE);
      double ddPct   = 0;
      if(Inp_InitialBalance > 0)
         ddPct = (Inp_InitialBalance - equity) / Inp_InitialBalance * 100.0;

      string ts = TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS);
      FileWrite(m_tradeFile,
         ts,
         IntegerToString(ticket),
         strategy,
         type,
         _Symbol,
         DoubleToString(entry, 5),
         DoubleToString(sl, 5),
         DoubleToString(tp, 5),
         DoubleToString(lots, 2),
         DoubleToString(riskPct, 2),
         DoubleToString(spreadPips, 1),
         DoubleToString(equity, 2),
         DoubleToString(balance, 2),
         DoubleToString(ddPct, 2),
         newsStatus,
         reason,
         "", "");
      FileFlush(m_tradeFile);
   }

   //+------------------------------------------------------------------+
   //| Log a trade close                                                 |
   //+------------------------------------------------------------------+
   void LogTradeClose(ulong ticket, string strategy, string type,
                      double entryPrice, double exitPrice, double lots,
                      double pnl)
   {
      if(!m_enabled || m_tradeFile == INVALID_HANDLE) return;

      double equity  = AccountInfoDouble(ACCOUNT_EQUITY);
      double balance = AccountInfoDouble(ACCOUNT_BALANCE);

      string ts = TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS);
      FileWrite(m_tradeFile,
         ts,
         IntegerToString(ticket),
         strategy,
         "CLOSE_" + type,
         _Symbol,
         DoubleToString(entryPrice, 5),
         "", "",
         DoubleToString(lots, 2),
         "",
         "",
         DoubleToString(equity, 2),
         DoubleToString(balance, 2),
         "", "", "",
         DoubleToString(pnl, 2),
         DoubleToString(exitPrice, 5));
      FileFlush(m_tradeFile);

      // Update daily stats
      m_dayTrades++;
      m_dayPnL += pnl;
      if(pnl > 0) m_dayWins++;
   }

   //+------------------------------------------------------------------+
   void Info(string msg)
   {
      Print("[FN-INFO] ", msg);
      WriteSystem("INFO", msg);
   }

   //+------------------------------------------------------------------+
   void Debug(string msg)
   {
      if(!m_verbose) return;
      Print("[FN-DEBUG] ", msg);
      WriteSystem("DEBUG", msg);
   }

   //+------------------------------------------------------------------+
   void Warning(string msg)
   {
      Print("[FN-WARN] ", msg);
      WriteSystem("WARN", msg);
   }

   //+------------------------------------------------------------------+
   void Error(string msg)
   {
      Print("[FN-ERROR] ", msg);
      WriteSystem("ERROR", msg);
   }

   //+------------------------------------------------------------------+
   //| Write daily summary (call at end of day or EA deinit)            |
   //+------------------------------------------------------------------+
   void WriteDailySummary()
   {
      if(!m_enabled) return;

      double balance = AccountInfoDouble(ACCOUNT_BALANCE);
      double wr = (m_dayTrades > 0) ? (double)m_dayWins / m_dayTrades * 100.0 : 0;

      string summary = StringFormat(
         "DAILY SUMMARY | Trades: %d | Wins: %d | WR: %.1f%% | PnL: $%.2f | Balance: $%.2f",
         m_dayTrades, m_dayWins, wr, m_dayPnL, balance);

      Info(summary);
   }

   //+------------------------------------------------------------------+
   void Flush()
   {
      if(m_tradeFile != INVALID_HANDLE)
      {
         FileFlush(m_tradeFile);
         FileClose(m_tradeFile);
         m_tradeFile = INVALID_HANDLE;
      }
      if(m_systemFile != INVALID_HANDLE)
      {
         FileFlush(m_systemFile);
         FileClose(m_systemFile);
         m_systemFile = INVALID_HANDLE;
      }
   }

private:
   //+------------------------------------------------------------------+
   void WriteSystem(string level, string msg)
   {
      if(!m_enabled || m_systemFile == INVALID_HANDLE) return;
      string ts = TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS);
      FileWriteString(m_systemFile, ts + " [" + level + "] " + msg + "\r\n");
   }
};

#endif
