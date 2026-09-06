import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ==========================================
# 1. KONFIGURASI HALAMAN & CSS STYLING
# ==========================================
st.set_page_config(
    page_title="Dashboard SIRAPI - PM Monitoring",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS untuk Tampilan Profesional
st.markdown("""
    <style>
    .main-header { font-size: 28px; font-weight: 800; color: #0f4c75; margin-bottom: -10px;}
    .sub-header { font-size: 16px; color: #6c757d; margin-bottom: 20px;}
    
    /* Styling untuk Metric Cards */
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
    </style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-header">📈 Dashboard Monitoring Kurva S (SIRAPI)</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">Monitoring Preventive Maintenance (PM Site & PM Genset) Terintegrasi</div>', unsafe_allow_html=True)

# ==========================================
# 2. FUNGSI PENGOLAH DATA KURVA S
# ==========================================
def calculate_scurve(df_filtered):
    df_filtered = df_filtered.copy()
    
    # Konversi ke datetime (handling error jika format salah)
    df_filtered['Schedule Date'] = pd.to_datetime(df_filtered['Schedule Date'], errors='coerce')
    df_filtered['Submitted Date'] = pd.to_datetime(df_filtered['Submitted Date'], errors='coerce')
    
    # Hapus baris yang tidak memiliki Schedule Date
    df_filtered = df_filtered.dropna(subset=['Schedule Date'])
    
    total_sites = len(df_filtered)
    if total_sites == 0:
        return None, 0, 0, 0, 0

    weight_per_site = 100.0 / total_sites
    
    min_sched = df_filtered['Schedule Date'].min()
    max_sched = df_filtered['Schedule Date'].max()
    
    # Definisi "Selesai" (SUBMITTED) - Bisa ditambahkan 'CLOSED' atau 'DONE' jika perlu
    df_submitted = df_filtered[df_filtered['Status'].astype(str).str.upper() == 'SUBMITTED']
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
    
    # --- TARGET ---
    target_daily = df_filtered.groupby('Schedule Date').size().reset_index(name='Target_Unit')
    timeline_df = pd.merge(timeline_df, target_daily, left_on='Date', right_on='Schedule Date', how='left')
    timeline_df['Target_Unit'] = timeline_df['Target_Unit'].fillna(0)
    timeline_df['Target_Bobot'] = timeline_df['Target_Unit'] * weight_per_site
    timeline_df['Target_Kumulatif'] = timeline_df['Target_Bobot'].cumsum()
    
    # --- ACTUAL ---
    if not df_submitted.empty:
        actual_daily = df_submitted.groupby('Submitted Date').size().reset_index(name='Actual_Unit')
        timeline_df = pd.merge(timeline_df, actual_daily, left_on='Date', right_on='Submitted Date', how='left')
        timeline_df['Actual_Unit'] = timeline_df['Actual_Unit'].fillna(0)
        timeline_df['Actual_Bobot'] = timeline_df['Actual_Unit'] * weight_per_site
        timeline_df['Actual_Kumulatif'] = timeline_df['Actual_Bobot'].cumsum()
        
        # Plot garis realisasi hanya sampai tanggal cutoff
        timeline_df['Actual_Kumulatif_Plot'] = timeline_df.apply(
            lambda r: r['Actual_Kumulatif'] if r['Date'] <= max_sub else np.nan, axis=1
        )
        current_actual_pct = timeline_df.loc[timeline_df['Date'] == max_sub, 'Actual_Kumulatif'].values[0] if max_sub else 0
        current_target_pct = timeline_df.loc[timeline_df['Date'] == max_sub, 'Target_Kumulatif'].values[0] if max_sub else 0
        deviation = current_actual_pct - current_target_pct
    else:
        timeline_df['Actual_Unit'] = 0
        timeline_df['Actual_Bobot'] = 0.0
        timeline_df['Actual_Kumulatif'] = 0.0
        timeline_df['Actual_Kumulatif_Plot'] = np.nan
        current_actual_pct = 0.0
        deviation = 0.0
        
    # Kolom Deviasi Harian untuk Tabel
    timeline_df['Deviasi'] = timeline_df['Actual_Kumulatif'] - timeline_df['Target_Kumulatif']

    return timeline_df, total_sites, completed_sites, current_actual_pct, deviation

# ==========================================
# 3. SIDEBAR & FILE UPLOAD (MULTI-FILE)
# ==========================================
st.sidebar.image("https://cdn-icons-png.flaticon.com/512/3256/3256013.png", width=60)
st.sidebar.header("📁 Upload Data")

# Menggunakan accept_multiple_files=True
uploaded_files = st.sidebar.file_uploader(
    "Upload File PM Site & PM Genset (Excel)", 
    type=["xlsx", "xls"], 
    accept_multiple_files=True
)

if uploaded_files:
    try:
        # Menggabungkan semua file yang diupload menjadi satu DataFrame
        df_list = []
        for file in uploaded_files:
            temp_df = pd.read_excel(file)
            temp_df['Sumber File'] = file.name # Menambahkan penanda file (PMS/PMG)
            df_list.append(temp_df)
            
        df_raw = pd.concat(df_list, ignore_index=True)
        
        # Validasi Kolom Minimum
        required_cols = ['Schedule Date', 'Status', 'NOP']
        missing_cols = [col for col in required_cols if col not in df_raw.columns]
        if missing_cols:
            st.error(f"File tidak valid. Kurang kolom berikut: {missing_cols}")
            st.stop()

        # ==========================================
        # 4. FILTER PARAMETER
        # ==========================================
        st.sidebar.markdown("---")
        st.sidebar.header("🔍 Filter Parameter")
        
        # Opsi Waiting Approval / Takeout
        st.sidebar.markdown("**Opsi Perhitungan Status:**")
        include_waiting = st.sidebar.checkbox("✅ Hitung 'Waiting Approval' sebagai Target", value=True, 
                                              help="Jika di-uncheck, site dengan status Waiting Approval akan dianggap Takeout dan dikeluarkan dari perhitungan.")

        # Filter Sumber File (Pemilih PMS / PMG / Total)
        list_sumber = ["Semua File (Gabungan)"] + sorted(df_raw['Sumber File'].unique().tolist())
        selected_sumber = st.sidebar.selectbox("Tipe PM (Sumber File):", list_sumber)

        # Filter NOP
        list_nop = ["Semua NOP"] + sorted([str(x) for x in df_raw['NOP'].dropna().unique()])
        selected_nop = st.sidebar.selectbox("Pilih NOP:", list_nop)
        
        # Filter Cluster (Jika Ada)
        if 'Cluster' in df_raw.columns:
            list_cluster = ["Semua Cluster"] + sorted([str(x) for x in df_raw['Cluster'].dropna().unique()])
            selected_cluster = st.sidebar.selectbox("Pilih Cluster:", list_cluster)
        else:
            selected_cluster = "Semua Cluster"

        # Proses Filtering Data
        df_filtered = df_raw.copy()
        
        # Terapkan filter File
        if selected_sumber != "Semua File (Gabungan)":
            df_filtered = df_filtered[df_filtered['Sumber File'] == selected_sumber]
            
        # Terapkan filter NOP & Cluster
        if selected_nop != "Semua NOP":
            df_filtered = df_filtered[df_filtered['NOP'] == selected_nop]
        if selected_cluster != "Semua Cluster":
            df_filtered = df_filtered[df_filtered['Cluster'] == selected_cluster]
            
        # Terapkan logika Waiting Approval
        if not include_waiting:
            # Sesuaikan string dengan penulisan di file excel Anda
            df_filtered = df_filtered[~df_filtered['Status'].astype(str).str.upper().str.contains('WAITING APPROVAL')]

        # ==========================================
        # 5. PERHITUNGAN & TAMPILAN DASHBOARD
        # ==========================================
        timeline_df, total_sites, completed_sites, current_actual_pct, deviation = calculate_scurve(df_filtered)

        if timeline_df is not None:
            
            # --- KPI Cards ---
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("📌 Total Target Site", f"{total_sites:,.0f} Unit", help="Total gabungan sesuai filter yang dipilih")
            col2.metric("✅ Site Selesai (Submitted)", f"{completed_sites:,.0f} Unit")
            col3.metric("📈 Progres Realisasi", f"{current_actual_pct:.2f}%")
            col4.metric("⚖️ Deviasi", f"{deviation:+.2f}%", delta_color="normal")

            st.markdown("<br>", unsafe_allow_html=True)

            # --- Grafik Kurva S (Plotly Professional) ---
            fig = make_subplots(specs=[[{"secondary_y": True}]])

            # Bar Chart Target (Background)
            fig.add_trace(
                go.Bar(
                    x=timeline_df['Date'],
                    y=timeline_df['Target_Unit'],
                    name='Target Harian (Unit)',
                    opacity=0.3,
                    marker_color='#cbd5e1', # Warna abu-abu elegan
                    hoverinfo='x+y'
                ),
                secondary_y=True
            )

            # Garis Target Kumulatif (Garis Putus-putus)
            fig.add_trace(
                go.Scatter(
                    x=timeline_df['Date'], 
                    y=timeline_df['Target_Kumulatif'],
                    mode='lines',
                    name='Plan Kumulatif (%)',
                    line=dict(color='#0f4c75', width=3, dash='dash'),
                ),
                secondary_y=False
            )

            # Garis Realisasi Kumulatif (Solid + Area bawah berwarna)
            fig.add_trace(
                go.Scatter(
                    x=timeline_df['Date'], 
                    y=timeline_df['Actual_Kumulatif_Plot'],
                    mode='lines+markers',
                    name='Actual Kumulatif (%)',
                    line=dict(color='#2ca02c', width=4),
                    marker=dict(size=6, color='#2ca02c'),
                    fill='tozeroy', # Mengisi warna area ke bawah grafik
                    fillcolor='rgba(44, 160, 44, 0.1)'
                ),
                secondary_y=False
            )

            # Styling Layout Grafik
            title_text = f"<b>S-Curve Gabungan PM (Monitoring)</b>"
            if selected_sumber != "Semua File (Gabungan)":
                title_text = f"<b>S-Curve {selected_sumber}</b>"

            fig.update_layout(
                title=dict(text=title_text, font=dict(size=20, color='#333333')),
                hovermode="x unified",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, bgcolor="rgba(255,255,255,0.8)"),
                height=500,
                margin=dict(l=40, r=40, t=60, b=40),
                plot_bgcolor='white',
                paper_bgcolor='white',
            )

            # Konfigurasi Grid & Axis
            fig.update_xaxes(title_text="", tickformat="%d %b '%y", showgrid=True, gridcolor='#f1f5f9', linecolor='#cbd5e1')
            fig.update_yaxes(title_text="Progres Kumulatif (%)", range=[0, 105], showgrid=True, gridcolor='#f1f5f9', linecolor='#cbd5e1', secondary_y=False)
            fig.update_yaxes(title_text="Volume (Unit)", showgrid=False, secondary_y=True)

            st.plotly_chart(fig, use_container_width=True)

            # --- Tabel Detail ---
            st.markdown("### 📋 Detail Data Ticket PM")
            tab1, tab2 = st.tabs(["📊 Tabel Rekap Harian (Kurva S)", "🗃️ Raw Data Ticket Terfilter"])
            
            with tab1:
                rekap_export = timeline_df[['Date', 'Target_Unit', 'Target_Kumulatif', 'Actual_Unit', 'Actual_Kumulatif', 'Deviasi']].copy()
                rekap_export.columns = ['Tanggal', 'Target Harian (Site)', 'Target Kumulatif (%)', 'Realisasi Harian (Site)', 'Realisasi Kumulatif (%)', 'Deviasi (%)']
                # Format decimal agar rapi di layar
                st.dataframe(rekap_export.style.format({
                    'Target Kumulatif (%)': '{:.2f}%',
                    'Realisasi Kumulatif (%)': '{:.2f}%',
                    'Deviasi (%)': '{:.2f}%'
                }), use_container_width=True)
            
            with tab2:
                st.dataframe(df_filtered, use_container_width=True)

        else:
            st.warning("⚠️ Data tidak ditemukan. Silakan cek kembali rentang tanggal, status, atau filter yang Anda pilih.")

    except Exception as e:
        st.error(f"❌ Terjadi kesalahan saat memproses file: {e}")

else:
    # Tampilan Awal (Kosong)
    st.info("👈 Silakan upload file Excel PM Site dan/atau PM Genset Anda pada sidebar sebelah kiri untuk memulai.")
    
    st.markdown("""
    **Panduan Penggunaan:**
    1. Anda dapat mengunggah **1 atau lebih file sekaligus** (misal: File PM Site.xlsx dan File PM Genset.xlsx).
    2. Sistem akan **menggabungkan (akumulasi)** seluruh total target dan realisasi secara otomatis.
    3. Anda dapat melihat grafik masing-masing file melalui filter **"Tipe PM (Sumber File)"**.
    4. Centang atau hilangkan centang opsi **"Waiting Approval"** pada filter untuk menghitung atau mengeluarkan site berstatus tersebut (*Takeout*).
    """)
