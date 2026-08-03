import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# Konfigurasi Halaman Streamlit
st.set_page_config(
    page_title="SIRAPI - Dashboard Monitoring PM & Kurva S",
    page_icon="📊",
    layout="wide"
)

# Custom Styling CSS
st.markdown("""
    <style>
    .main-header { font-size: 24px; font-weight: bold; color: #1F4E78; }
    .stMetric { background-color: #f8f9fa; padding: 10px; border-radius: 8px; border-left: 4px solid #1F4E78; }
    </style>
""", unsafe_allow_html=True)

st.title("📊 Dashboard Monitoring PM Site & Kurva S (SIRAPI)")
st.caption("Upload file Excel PM Site untuk melihat Kurva S dan progres realisasi secara otomatis.")

# 1. Widget Upload File Excel
uploaded_file = st.sidebar.file_uploader(
    "📁 Upload File Excel PM (xlsx / xls)", 
    type=["xlsx", "xls"]
)

# Fungsi Pengolah Data Kurva S
def calculate_scurve(df_filtered):
    df_filtered = df_filtered.copy()
    df_filtered['Schedule Date'] = pd.to_datetime(df_filtered['Schedule Date'])
    df_filtered['Submitted Date'] = pd.to_datetime(df_filtered['Submitted Date'])
    
    total_sites = len(df_filtered)
    if total_sites == 0:
        return None, 0, 0, 0
    
    # Hitung bobot dinamis per site berdasarkan total terfilter
    weight_per_site = 100.0 / total_sites
    
    # Tentukan rentang tanggal terawal dan terakhir
    min_sched = df_filtered['Schedule Date'].min()
    max_sched = df_filtered['Schedule Date'].max()
    
    df_submitted = df_filtered[df_filtered['Status'].astype(str).str.upper() == 'SUBMITTED']
    completed_sites = len(df_submitted)
    
    if not df_submitted.empty:
        min_sub = df_submitted['Submitted Date'].min()
        max_sub = df_submitted['Submitted Date'].max()
        start_date = min(min_sched, min_sub)
        end_date = max(max_sched, max_sub)
    else:
        start_date = min_sched
        end_date = max_sched
        max_sub = None
        
    all_dates = pd.date_range(start=start_date, end=end_date, freq='D')
    timeline_df = pd.DataFrame({'Date': all_dates})
    
    # Target Daily & Kumulatif
    target_daily = df_filtered.groupby('Schedule Date').size().reset_index(name='Target_Unit')
    timeline_df = pd.merge(timeline_df, target_daily, left_on='Date', right_on='Schedule Date', how='left')
    timeline_df['Target_Unit'] = timeline_df['Target_Unit'].fillna(0)
    timeline_df['Target_Bobot'] = timeline_df['Target_Unit'] * weight_per_site
    timeline_df['Target_Kumulatif'] = timeline_df['Target_Bobot'].cumsum()
    
    # Realisasi Daily & Kumulatif
    if not df_submitted.empty:
        actual_daily = df_submitted.groupby('Submitted Date').size().reset_index(name='Actual_Unit')
        timeline_df = pd.merge(timeline_df, actual_daily, left_on='Date', right_on='Submitted Date', how='left')
        timeline_df['Actual_Unit'] = timeline_df['Actual_Unit'].fillna(0)
        timeline_df['Actual_Bobot'] = timeline_df['Actual_Unit'] * weight_per_site
        timeline_df['Actual_Kumulatif'] = timeline_df['Actual_Bobot'].cumsum()
        
        # Plot garis realisasi hanya sampai tanggal cutoff submitted terakhir
        timeline_df['Actual_Kumulatif_Plot'] = timeline_df.apply(
            lambda r: r['Actual_Kumulatif'] if r['Date'] <= max_sub else np.nan, axis=1
        )
        current_actual_pct = timeline_df.loc[timeline_df['Date'] == max_sub, 'Actual_Kumulatif'].values[0]
        current_target_pct = timeline_df.loc[timeline_df['Date'] == max_sub, 'Target_Kumulatif'].values[0]
        deviation = current_actual_pct - current_target_pct
    else:
        timeline_df['Actual_Unit'] = 0
        timeline_df['Actual_Bobot'] = 0.0
        timeline_df['Actual_Kumulatif'] = 0.0
        timeline_df['Actual_Kumulatif_Plot'] = np.nan
        current_actual_pct = 0.0
        deviation = 0.0

    return timeline_df, total_sites, completed_sites, current_actual_pct, deviation

# Logika Utama Aplikasi
if uploaded_file is not None:
    try:
        # Load File
        df_raw = pd.read_excel(uploaded_file)
        
        # Validasi Kolom Minimum
        required_cols = ['Schedule Date', 'Status', 'NOP']
        if not all(col in df_raw.columns for col in required_cols):
            st.error(f"File harus memiliki kolom dasar: {required_cols}")
            st.stop()

        # 2. Sidebar Filters
        st.sidebar.header("🔍 Filter Parameter")
        
        # Filter NOP
        list_nop = ["Semua NOP"] + sorted([str(x) for x in df_raw['NOP'].dropna().unique()])
        selected_nop = st.sidebar.selectbox("Pilih NOP:", list_nop)
        
        # Filter Cluster
        if 'Cluster' in df_raw.columns:
            list_cluster = ["Semua Cluster"] + sorted([str(x) for x in df_raw['Cluster'].dropna().unique()])
            selected_cluster = st.sidebar.selectbox("Pilih Cluster:", list_cluster)
        else:
            selected_cluster = "Semua Cluster"

        # Filter Status
        list_status = ["Semua Status"] + sorted([str(x) for x in df_raw['Status'].dropna().unique()])
        selected_status = st.sidebar.selectbox("Pilih Status:", list_status)

        # Proses Filtering Data
        df_filtered = df_raw.copy()
        if selected_nop != "Semua NOP":
            df_filtered = df_filtered[df_filtered['NOP'] == selected_nop]
        if selected_cluster != "Semua Cluster":
            df_filtered = df_filtered[df_filtered['Cluster'] == selected_cluster]
        if selected_status != "Semua Status":
            df_filtered = df_filtered[df_filtered['Status'] == selected_status]

        # 3. Hitung Kurva S
        timeline_df, total_sites, completed_sites, current_actual_pct, deviation = calculate_scurve(df_filtered)

        if timeline_df is not None:
            # 4. Ringkasan KPI Cards
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Total Target Site", f"{total_sites} Site")
            col2.metric("Site Selesai (Submitted)", f"{completed_sites} Site")
            col3.metric("Progres Realisasi", f"{current_actual_pct:.2f}%")
            col4.metric("Deviasi Progress", f"{deviation:+.2f}%", delta_color="normal")

            st.divider()

            # 5. Render Grafik Kurva S Interaktif dengan Plotly
            fig = make_subplots(specs=[[{"secondary_y": True}]])

            # Garis Target Kumulatif (S-Curve)
            fig.add_trace(
                go.Scatter(
                    x=timeline_df['Date'], 
                    y=timeline_df['Target_Kumulatif'],
                    mode='lines+markers',
                    name='Target Kumulatif (%)',
                    line=dict(color='#1f77b4', width=3),
                    marker=dict(size=6)
                ),
                secondary_y=False
            )

            # Garis Realisasi Kumulatif (S-Curve)
            fig.add_trace(
                go.Scatter(
                    x=timeline_df['Date'], 
                    y=timeline_df['Actual_Kumulatif_Plot'],
                    mode='lines+markers',
                    name='Realisasi Kumulatif (%)',
                    line=dict(color='#2ca02c', width=3),
                    marker=dict(size=8, symbol='square')
                ),
                secondary_y=False
            )

            # Bar Chart Target Volume Harian
            fig.add_trace(
                go.Bar(
                    x=timeline_df['Date'],
                    y=timeline_df['Target_Unit'],
                    name='Target Site / Hari',
                    opacity=0.25,
                    marker_color='#1f77b4'
                ),
                secondary_y=True
            )

            # Layout Styling
            fig.update_layout(
                title_text=f"<b>KURVA S MONITORING PREVENTIVE MAINTENANCE</b><br><sup>Filter: NOP ({selected_nop}) | Cluster ({selected_cluster})</sup>",
                hovermode="x unified",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                height=550,
                template="plotly_white"
            )

            fig.update_xaxes(title_text="Tanggal", tickformat="%d %b %Y")
            fig.update_yaxes(title_text="Progres Kumulatif (%)", range=[0, 105], secondary_y=False)
            fig.update_yaxes(title_text="Jumlah Site (Unit)", showgrid=False, secondary_y=True)

            st.plotly_chart(fig, use_container_width=True)

            # 6. Tabel Data Detail & Download
            st.subheader("📋 Detail Data Ticket PM Terfilter")
            
            tab1, tab2 = st.tabs(["Data Ticket", "Tabel Rekap Harian Kurva S"])
            
            with tab1:
                st.dataframe(df_filtered, use_container_width=True)
            
            with tab2:
                rekap_export = timeline_df[['Date', 'Target_Unit', 'Target_Kumulatif', 'Actual_Unit', 'Actual_Kumulatif', 'Deviasi']].copy()
                rekap_export.columns = ['Tanggal', 'Target (Site)', 'Target Kumulatif (%)', 'Realisasi (Site)', 'Realisasi Kumulatif (%)', 'Deviasi (%)']
                st.dataframe(rekap_export, use_container_width=True)

        else:
            st.warning("Data tidak ditemukan untuk kombinasi filter yang dipilih.")

    except Exception as e:
        st.error(f"Terjadi kesalahan saat memproses file Excel: {e}")
else:
    # Tampilan Awal saat File Belum Upload
    st.info("👈 Silakan upload file Excel PM Site Anda pada sidebar sebelah kiri untuk memulai.")
    
    st.markdown("""
    ### Catatan Format Kolom Excel:
    Pastikan file Excel yang di-upload mengandung kolom-kolom berikut:
    * *NOP* (misal: NOP PALANGKARAYA, NOP PONTIANAK, dll)
    * *Schedule Date* (Tanggal Rencana PM)
    * *Submitted Date* (Tanggal Selesai PM)
    * *Status* (SUBMITTED, ASSIGNED, IN PROGRESS, NEW)
    * *Site / Ticket No* (ID unik pekerjaan)
    """)
