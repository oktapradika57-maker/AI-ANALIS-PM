import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ==========================================
# 1. KONFIGURASI HALAMAN & CSS STYLING
# ==========================================
st.set_page_config(
    page_title="Dashboard SiPM - PM Monitoring",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS
st.markdown("""
    <style>
    .main-header { font-size: 28px; font-weight: 800; color: #0f4c75; margin-bottom: -10px;}
    .sub-header { font-size: 16px; color: #6c757d; margin-bottom: 20px;}
    
    div[data-testid="metric-container"] {
        background-color: #ffffff;
        border: 1px solid #e0e0e0;
        padding: 15px 20px;
        border-radius: 10px;
        box-shadow: 2px 4px 10px rgba(0, 0, 0, 0.05);
        border-left: 5px solid #0f4c75;
    }
    div[data-testid="metric-container"] label {
        font-size: 14px;
        font-weight: 600;
        color: #333333;
    }
    .report-text {
        background-color: #e6f7ff; border-left: 4px solid #1890ff; padding: 15px; border-radius: 5px; font-size: 15px; margin-bottom: 20px;
    }
    </style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-header">📈 Dashboard Monitoring Kurva S & Leaderboard (SIRAPI)</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">Monitoring Preventive Maintenance Terpisah (PMS & PMG)</div>', unsafe_allow_html=True)

# ==========================================
# 2. FUNGSI PENGOLAH DATA UTAMA
# ==========================================
def assign_tipe_pm(filename):
    f_upper = str(filename).upper()
    if 'SITE' in f_upper or 'PMS' in f_upper:
        return 'PMS'
    elif 'GENSET' in f_upper or 'PMG' in f_upper:
        return 'PMG'
    return 'Lainnya'

def calculate_scurve(df_filtered, completed_statuses):
    df_filtered = df_filtered.copy()
    
    df_filtered['Schedule Date'] = pd.to_datetime(df_filtered['Schedule Date'], errors='coerce')
    df_filtered['Submitted Date'] = pd.to_datetime(df_filtered['Submitted Date'], errors='coerce')
    df_filtered = df_filtered.dropna(subset=['Schedule Date'])
    
    total_sites = len(df_filtered)
    if total_sites == 0:
        return None, 0, 0, 0

    weight_per_site = 100.0 / total_sites
    min_sched = df_filtered['Schedule Date'].min()
    max_sched = df_filtered['Schedule Date'].max()
    
    # Hanya hitung aktual/selesai dari status yang DIPILIH (multiselect Selesai)
    df_submitted = df_filtered[df_filtered['Status'].astype(str).str.upper().isin([s.upper() for s in completed_statuses])]
    completed_sites = len(df_submitted)
    
    if not df_submitted.empty:
        min_sub = df_submitted['Submitted Date'].min()
        max_sub = df_submitted['Submitted Date'].max()
        start_date = min(min_sched, min_sub) if pd.notnull(min_sub) else min_sched
        end_date = max(max_sched, max_sub) if pd.notnull(max_sub) else max_sched
    else:
        start_date = min_sched
        end_date = max_sched
        max_sub = None
        
    all_dates = pd.date_range(start=start_date, end=end_date, freq='D')
    timeline_df = pd.DataFrame({'Date': all_dates})
    
    # TARGET
    target_daily = df_filtered.groupby('Schedule Date').size().reset_index(name='Target_Unit')
    timeline_df = pd.merge(timeline_df, target_daily, left_on='Date', right_on='Schedule Date', how='left')
    timeline_df['Target_Unit'] = timeline_df['Target_Unit'].fillna(0)
    timeline_df['Target_Kumulatif'] = (timeline_df['Target_Unit'] * weight_per_site).cumsum()
    
    # ACTUAL
    if not df_submitted.empty:
        actual_daily = df_submitted.groupby('Submitted Date').size().reset_index(name='Actual_Unit')
        timeline_df = pd.merge(timeline_df, actual_daily, left_on='Date', right_on='Submitted Date', how='left')
        timeline_df['Actual_Unit'] = timeline_df['Actual_Unit'].fillna(0)
        timeline_df['Actual_Kumulatif'] = (timeline_df['Actual_Unit'] * weight_per_site).cumsum()
        
        timeline_df['Actual_Kumulatif_Plot'] = timeline_df.apply(
            lambda r: r['Actual_Kumulatif'] if r['Date'] <= max_sub else np.nan, axis=1
        )
        current_actual_pct = timeline_df.loc[timeline_df['Date'] == max_sub, 'Actual_Kumulatif'].values[0] if max_sub else 0
    else:
        timeline_df['Actual_Unit'] = 0
        timeline_df['Actual_Kumulatif'] = 0.0
        timeline_df['Actual_Kumulatif_Plot'] = np.nan
        current_actual_pct = 0.0
        
    return timeline_df, total_sites, completed_sites, current_actual_pct

def generate_leaderboard(df_filtered, completed_statuses):
    if df_filtered.empty: return pd.DataFrame()

    agg_nop = df_filtered.groupby('NOP').agg(
        Count_Site=('NOP', 'count'),
        Total_Closed=('Status', lambda x: x.astype(str).str.upper().isin([s.upper() for s in completed_statuses]).sum())
    ).reset_index()
    agg_nop['% Ach'] = (agg_nop['Total_Closed'] / agg_nop['Count_Site'] * 100).fillna(0)
    agg_nop['Rank'] = agg_nop['% Ach'].rank(method='min', ascending=False).astype(int)

    agg_detail = df_filtered.groupby(['NOP', 'Tipe_PM']).agg(
        Count_Site=('NOP', 'count'),
        Total_Closed=('Status', lambda x: x.astype(str).str.upper().isin([s.upper() for s in completed_statuses]).sum())
    ).reset_index()
    agg_detail['% Ach'] = (agg_detail['Total_Closed'] / agg_detail['Count_Site'] * 100).fillna(0)

    records = []
    grand_site, grand_closed = 0, 0

    for _, row in agg_nop.sort_values('Rank').iterrows():
        nop_name = row['NOP']
        records.append({
            'NOP': f"⊟ {nop_name}", 'PM Status': 'TOTAL NOP',
            'Target': row['Count_Site'], 'Terealisasi': row['Total_Closed'],
            '% Ach': row['% Ach'] / 100.0, 'Rank': row['Rank']
        })
        grand_site += row['Count_Site']
        grand_closed += row['Total_Closed']

        details = agg_detail[agg_detail['NOP'] == nop_name]
        for _, d_row in details.iterrows():
            records.append({
                'NOP': "", 'PM Status': d_row['Tipe_PM'],
                'Target': d_row['Count_Site'], 'Terealisasi': d_row['Total_Closed'],
                '% Ach': d_row['% Ach'] / 100.0, 'Rank': ""
            })

    records.append({
        'NOP': 'Grand Total', 'PM Status': '', 'Target': grand_site,
        'Terealisasi': grand_closed, '% Ach': (grand_closed/grand_site if grand_site > 0 else 0), 'Rank': ''
    })

    return pd.DataFrame(records)

def draw_scurve_chart(timeline_df, title, unit_text):
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(x=timeline_df['Date'], y=timeline_df['Target_Unit'], name=f'Target Harian ({unit_text})', opacity=0.3, marker_color='#cbd5e1', hoverinfo='x+y'), secondary_y=True)
    fig.add_trace(go.Scatter(x=timeline_df['Date'], y=timeline_df['Target_Kumulatif'], mode='lines', name='Plan Kumulatif (%)', line=dict(color='#0f4c75', width=3, dash='dash')), secondary_y=False)
    fig.add_trace(go.Scatter(x=timeline_df['Date'], y=timeline_df['Actual_Kumulatif_Plot'], mode='lines+markers', name='Actual Kumulatif (%)', line=dict(color='#2ca02c', width=4), marker=dict(size=6, color='#2ca02c'), fill='tozeroy', fillcolor='rgba(44, 160, 44, 0.1)'), secondary_y=False)
    
    fig.update_layout(title=dict(text=title, font=dict(size=18, color='#333333')), hovermode="x unified", legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1), height=450, margin=dict(l=30, r=30, t=50, b=30), plot_bgcolor='white', paper_bgcolor='white')
    fig.update_xaxes(title_text="", tickformat="%d %b", showgrid=True, gridcolor='#f1f5f9', linecolor='#cbd5e1')
    fig.update_yaxes(title_text="Progres (%)", range=[0, 105], showgrid=True, gridcolor='#f1f5f9', linecolor='#cbd5e1', secondary_y=False)
    fig.update_yaxes(title_text=f"Volume", showgrid=False, secondary_y=True)
    return fig

# ==========================================
# 3. SIDEBAR & FILE UPLOAD
# ==========================================
st.sidebar.image("https://cdn-icons-png.flaticon.com/512/3256/3256013.png", width=60)
st.sidebar.header("📁 Upload Data")

uploaded_files = st.sidebar.file_uploader("Upload File PM Site & PM Genset (Excel)", type=["xlsx", "xls"], accept_multiple_files=True)

if uploaded_files:
    try:
        df_list = []
        for file in uploaded_files:
            temp_df = pd.read_excel(file)
            temp_df['Sumber File'] = file.name
            temp_df['Tipe_PM'] = assign_tipe_pm(file.name)
            df_list.append(temp_df)
            
        df_raw = pd.concat(df_list, ignore_index=True)
        all_unique_statuses = sorted(df_raw['Status'].dropna().astype(str).str.upper().unique().tolist())

        # ==========================================
        # 4. FILTER PENGATURAN LOGIKA KURVA (MURNI MULTISELECT)
        # ==========================================
        st.sidebar.markdown("---")
        st.sidebar.header("🔍 Pengaturan Logika Kurva")
        
        # Multiselect 1: Status Selesai (Realisasi)
        default_completed = [s for s in ['SUBMITTED', 'CLOSED', 'DONE', 'APPROVED', 'WAITING AMNESTY'] if s in all_unique_statuses]
        if not default_completed and all_unique_statuses:
            default_completed = [all_unique_statuses[0]]
            
        selected_completed = st.sidebar.multiselect(
            "1. Status Dihitung Selesai (Realisasi):",
            options=all_unique_statuses,
            default=default_completed,
            help="Menjadi pembentuk Kurva S Aktual/Realisasi."
        )
        
        # Multiselect 2: Status Takeout (Diabaikan)
        default_takeout = [s for s in all_unique_statuses if 'TAKEOUT' in s or 'WAITING APPROVAL' in s]
        selected_takeout = st.sidebar.multiselect(
            "2. Status Diabaikan (Takeout dari Target):",
            options=all_unique_statuses,
            default=default_takeout,
            help="Status yang dipilih di sini TIDAK AKAN DIHITUNG sama sekali ke dalam total target."
        )

        st.sidebar.markdown("---")
        st.sidebar.header("🎯 Filter Area & Tipe")
        list_nop = sorted([str(x) for x in df_raw['NOP'].dropna().unique()])
        selected_nop = st.sidebar.multiselect("Pilih NOP:", list_nop, default=list_nop)

        # --- PROSES FILTERING UTAMA ---
        df_filtered = df_raw.copy()
        
        # Filter berdasarkan NOP
        if selected_nop: 
            df_filtered = df_filtered[df_filtered['NOP'].isin(selected_nop)]
        
        # Buang data yang masuk dalam kategori "Diabaikan (Takeout)"
        if selected_takeout:
            df_filtered = df_filtered[~df_filtered['Status'].astype(str).str.upper().isin([s.upper() for s in selected_takeout])]

        # Hitung Nilai Aktual Terhadap Status Pilihan
        df_filtered['Is_Selesai'] = df_filtered['Status'].astype(str).str.upper().isin([s.upper() for s in selected_completed])

        # ==========================================
        # 5. GENERATE REPORT TEXT (AUTO HIGHLIGHT)
        # ==========================================
        df_sub_only = df_filtered[df_filtered['Is_Selesai']].copy()
        if not df_sub_only.empty:
            df_sub_only['Submitted Date'] = pd.to_datetime(df_sub_only['Submitted Date'], errors='coerce')
            max_date = df_sub_only['Submitted Date'].max()
            if pd.notnull(max_date):
                df_last = df_sub_only[df_sub_only['Submitted Date'] == max_date]
                count_pms = len(df_last[df_last['Tipe_PM'] == 'PMS'])
                count_pmg = len(df_last[df_last['Tipe_PM'] == 'PMG'])
                total_last = count_pms + count_pmg
                
                date_str = max_date.strftime('%d %B %Y')
                report_text = f"📢 **Daily Highlight:** Berdasarkan update terakhir per tanggal **{date_str}**, terdapat penambahan realisasi sebanyak **{total_last} Unit**, dengan rincian **{count_pms} PMS** dan **{count_pmg} PMG** yang disubmit."
                st.markdown(f'<div class="report-text">{report_text}</div>', unsafe_allow_html=True)

        # ==========================================
        # 6. PEMISAHAN TARGET (KPI CARDS)
        # ==========================================
        tgt_pms = len(df_filtered[df_filtered['Tipe_PM'] == 'PMS'])
        tgt_pmg = len(df_filtered[df_filtered['Tipe_PM'] == 'PMG'])
        tgt_total = tgt_pms + tgt_pmg
        
        act_pms = len(df_filtered[(df_filtered['Tipe_PM'] == 'PMS') & (df_filtered['Is_Selesai'])])
        act_pmg = len(df_filtered[(df_filtered['Tipe_PM'] == 'PMG') & (df_filtered['Is_Selesai'])])
        act_total = act_pms + act_pmg
        
        pct_total = (act_total / tgt_total * 100) if tgt_total > 0 else 0

        col1, col2, col3 = st.columns(3)
        col1.metric("🎯 Total Target Pekerjaan", f"{tgt_total} Unit", f"PMS: {tgt_pms} Site | PMG: {tgt_pmg} Genset", delta_color="off")
        col2.metric("✅ Total Terealisasi", f"{act_total} Unit", f"PMS: {act_pms} Site | PMG: {act_pmg} Genset", delta_color="off")
        col3.metric("📈 Progres Keseluruhan", f"{pct_total:.2f}%")

        st.markdown("<hr style='margin: 15px 0px 25px 0px;'>", unsafe_allow_html=True)

        # ==========================================
        # 7. SPLIT GRAFIK PMS & PMG BERDAMPINGAN
        # ==========================================
        st.markdown("### 📊 Tren Kurva S (PMS vs PMG)")
        
        df_pms = df_filtered[df_filtered['Tipe_PM'] == 'PMS']
        df_pmg = df_filtered[df_filtered['Tipe_PM'] == 'PMG']

        col_chart1, col_chart2 = st.columns(2)
        
        with col_chart1:
            if not df_pms.empty:
                tl_pms, _, _, _ = calculate_scurve(df_pms, selected_completed)
                st.plotly_chart(draw_scurve_chart(tl_pms, "S-Curve PM Site (PMS)", "Site"), use_container_width=True)
            else:
                st.info("⚠️ Data PMS tidak tersedia pada filter saat ini.")

        with col_chart2:
            if not df_pmg.empty:
                tl_pmg, _, _, _ = calculate_scurve(df_pmg, selected_completed)
                st.plotly_chart(draw_scurve_chart(tl_pmg, "S-Curve PM Genset (PMG)", "Genset"), use_container_width=True)
            else:
                st.info("⚠️ Data PMG tidak tersedia pada filter saat ini.")

        st.markdown("<br>", unsafe_allow_html=True)

        # ==========================================
        # 8. TABEL DAILY SUBMITTED (BERJENJANG TANGGAL)
        # ==========================================
        st.markdown("### 📅 Rekap Daily Submitted Berjenjang")
        if not df_sub_only.empty:
            df_daily = df_sub_only.groupby(['Submitted Date', 'Tipe_PM']).size().unstack(fill_value=0).reset_index()
            if 'PMS' not in df_daily.columns: df_daily['PMS'] = 0
            if 'PMG' not in df_daily.columns: df_daily['PMG'] = 0
            
            df_daily['Total Disubmit'] = df_daily['PMS'] + df_daily['PMG']
            df_daily = df_daily.sort_values('Submitted Date', ascending=False)
            df_daily['Submitted Date'] = df_daily['Submitted Date'].dt.strftime('%d %B %Y')
            df_daily.columns = ['Tanggal Submit', 'Selesai PMG (Genset)', 'Selesai PMS (Site)', 'Total Disubmit Hari Itu']
            
            df_daily = df_daily[['Tanggal Submit', 'Selesai PMS (Site)', 'Selesai PMG (Genset)', 'Total Disubmit Hari Itu']]
            st.dataframe(df_daily, use_container_width=True, hide_index=True)
        else:
            st.warning("Belum ada data dengan status terealisasi (selesai).")

        st.markdown("<br>", unsafe_allow_html=True)

        # ==========================================
        # 9. LEADERBOARD & DETAIL DATA
        # ==========================================
        st.markdown("### 🏆 Peringkat Pencapaian per NOP")
        df_leaderboard = generate_leaderboard(df_filtered, selected_completed)
        
        def style_leaderboard(row):
            if row['NOP'] == 'Grand Total':
                return ['background-color: #d9e1f2; font-weight: bold; color: black; border-top: 2px solid #0f4c75;'] * len(row)
            elif row['PM Status'] == 'TOTAL NOP':
                return ['background-color: #f8f9fa; font-weight: bold; color: #0f4c75; border-top: 1px solid #dee2e6;'] * len(row)
            else:
                return ['background-color: #ffffff; color: #495057;'] * len(row)

        if not df_leaderboard.empty:
            styled_df = df_leaderboard.style.apply(style_leaderboard, axis=1).format({"% Ach": "{:.0%}"})
            st.dataframe(styled_df, use_container_width=True, hide_index=True, height=int(35.2 * (len(df_leaderboard) + 1)))

    except Exception as e:
        st.error(f"❌ Terjadi kesalahan saat memproses file: {e}")

else:
    st.info("👈 Silakan upload file Excel PM Site dan PM Genset Anda pada sidebar sebelah kiri untuk memulai.")
