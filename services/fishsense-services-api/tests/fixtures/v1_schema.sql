-- v1 (fishsense-lite) schema, schema only -- no data. Extracted from the
-- 2026-09-25 nightly dump with pg_dump --schema-only; psql meta-commands and the
-- empty search_path removed. Test fixture for the v1 -> v2 migration job.

--
-- PostgreSQL database dump
--


-- Dumped from database version 17.10 (Debian 17.10-1.pgdg13+1)
-- Dumped by pg_dump version 17.10 (Debian 17.10-1.pgdg13+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: cameracalibrationtype; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.cameracalibrationtype AS ENUM (
    'CAMERA_INTRINSICS'
);


--
-- Name: datasource; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.datasource AS ENUM (
    'PREDICTION',
    'LABEL_STUDIO'
);


--
-- Name: priority; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.priority AS ENUM (
    'LOW',
    'HIGH',
    'NONE'
);


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


--
-- Name: calibrationtarget; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.calibrationtarget (
    id integer NOT NULL,
    name character varying(100) NOT NULL,
    rows integer NOT NULL,
    cols integer NOT NULL,
    square_size_m double precision NOT NULL,
    notes character varying,
    created_at timestamp with time zone
);


--
-- Name: calibrationtarget_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.calibrationtarget_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: calibrationtarget_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.calibrationtarget_id_seq OWNED BY public.calibrationtarget.id;


--
-- Name: camera; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.camera (
    id integer NOT NULL,
    serial_number character varying NOT NULL,
    name character varying NOT NULL
);


--
-- Name: camera_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.camera_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: camera_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.camera_id_seq OWNED BY public.camera.id;


--
-- Name: cameraintrinsics; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cameraintrinsics (
    id integer NOT NULL,
    camera_matrix json,
    distortion_coefficients json,
    camera_id integer NOT NULL
);


--
-- Name: cameraintrinsics_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.cameraintrinsics_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: cameraintrinsics_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.cameraintrinsics_id_seq OWNED BY public.cameraintrinsics.id;


--
-- Name: dive; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dive (
    id integer NOT NULL,
    path character varying(255) NOT NULL,
    dive_datetime timestamp with time zone NOT NULL,
    priority public.priority,
    camera_id integer,
    dive_slate_id integer,
    flip_dive_slate boolean,
    name character varying,
    calibration_dive_id integer,
    notes character varying,
    calibration_target_id integer,
    calibration_refused_at timestamp with time zone,
    calibration_refused_reason character varying,
    calibration_refused_labels_at timestamp with time zone
);


--
-- Name: dive_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.dive_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: dive_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.dive_id_seq OWNED BY public.dive.id;


--
-- Name: diveframecluster; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.diveframecluster (
    id integer NOT NULL,
    dive_id integer,
    data_source public.datasource,
    updated_at timestamp with time zone,
    fish_id integer
);


--
-- Name: diveframeclusterimagemapping; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.diveframeclusterimagemapping (
    dive_frame_cluster_id integer NOT NULL,
    image_id integer NOT NULL
);


--
-- Name: diveslatelabel; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.diveslatelabel (
    id integer NOT NULL,
    label_studio_task_id integer,
    label_studio_project_id integer,
    image_url character varying,
    updated_at timestamp with time zone,
    completed boolean,
    label_studio_json json,
    image_id integer,
    user_id integer,
    upside_down boolean,
    reference_points json,
    slate_rectangle json,
    skipped_points json,
    superseded boolean,
    needs_reprocess boolean DEFAULT false NOT NULL
);


--
-- Name: headtaillabel; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.headtaillabel (
    id integer NOT NULL,
    label_studio_task_id integer,
    head_x double precision,
    head_y double precision,
    tail_x double precision,
    tail_y double precision,
    image_id integer,
    user_id integer,
    updated_at timestamp with time zone,
    completed boolean,
    label_studio_json json,
    label_studio_project_id integer,
    superseded boolean,
    needs_reprocess boolean DEFAULT false NOT NULL
);


--
-- Name: image; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.image (
    id integer NOT NULL,
    path character varying(255) NOT NULL,
    taken_datetime timestamp with time zone NOT NULL,
    checksum character varying(32) NOT NULL,
    is_canonical boolean NOT NULL,
    dive_id integer,
    camera_id integer
);


--
-- Name: laserextrinsics; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.laserextrinsics (
    id integer NOT NULL,
    laser_position json,
    laser_axis json,
    created_at timestamp with time zone DEFAULT now(),
    dive_id integer,
    camera_id integer NOT NULL
);


--
-- Name: laserlabel; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.laserlabel (
    id integer NOT NULL,
    label_studio_task_id integer,
    x double precision,
    y double precision,
    label character varying,
    image_id integer,
    user_id integer,
    updated_at timestamp with time zone,
    completed boolean,
    label_studio_json json,
    label_studio_project_id integer,
    superseded boolean,
    needs_reprocess boolean DEFAULT false NOT NULL
);


--
-- Name: measurement; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.measurement (
    id integer NOT NULL,
    length_m double precision,
    image_id integer,
    fish_id integer,
    laser_extrinsics_id integer
);


--
-- Name: specieslabel; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.specieslabel (
    id integer NOT NULL,
    label_studio_task_id integer,
    updated_at timestamp with time zone,
    completed boolean,
    label_studio_json json,
    image_id integer,
    user_id integer,
    image_url character varying,
    label_studio_project_id integer,
    top_three_photos_of_group boolean,
    slate_upside_down boolean,
    laser_x double precision,
    laser_y double precision,
    laser_label character varying,
    content_of_image character varying,
    fish_measurable_category character varying,
    fish_angle_category character varying,
    fish_curved_category character varying,
    "grouping" character varying,
    superseded boolean,
    needs_reprocess boolean DEFAULT false NOT NULL,
    fish_angle_degrees double precision
);


--
-- Name: dive_pipeline_status; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.dive_pipeline_status AS
 SELECT id AS dive_id,
    name AS dive_name,
    priority,
    dive_slate_id,
    ((EXISTS ( SELECT 1
           FROM public.image i
          WHERE ((i.dive_id = d.id) AND i.is_canonical))) AND (NOT (EXISTS ( SELECT 1
           FROM public.image i
          WHERE ((i.dive_id = d.id) AND i.is_canonical AND (NOT (EXISTS ( SELECT 1
                   FROM public.laserlabel ll
                  WHERE (ll.image_id = i.id))))))))) AS laser_preprocessed,
    ((EXISTS ( SELECT 1
           FROM (public.laserlabel ll
             JOIN public.image i ON ((i.id = ll.image_id)))
          WHERE ((i.dive_id = d.id) AND i.is_canonical AND (ll.superseded = false) AND (ll.completed = true)))) AND (NOT (EXISTS ( SELECT 1
           FROM (public.laserlabel ll
             JOIN public.image i ON ((i.id = ll.image_id)))
          WHERE ((i.dive_id = d.id) AND i.is_canonical AND (ll.superseded = false) AND ((ll.completed = false) OR (ll.completed IS NULL))))))) AS laser_labeling_complete,
    ((EXISTS ( SELECT 1
           FROM (public.laserlabel ll
             JOIN public.image i ON ((i.id = ll.image_id)))
          WHERE ((i.dive_id = d.id) AND i.is_canonical AND (ll.completed = true) AND (ll.superseded = false) AND (ll.x IS NOT NULL) AND (ll.y IS NOT NULL)))) AND (NOT (EXISTS ( SELECT 1
           FROM (public.laserlabel ll
             JOIN public.image i ON ((i.id = ll.image_id)))
          WHERE ((i.dive_id = d.id) AND i.is_canonical AND (ll.completed = true) AND (ll.superseded = false) AND (ll.x IS NOT NULL) AND (ll.y IS NOT NULL) AND (NOT (EXISTS ( SELECT 1
                   FROM public.headtaillabel htl
                  WHERE ((htl.image_id = i.id) AND (htl.label_studio_project_id IS NOT NULL)))))))))) AS headtail_preprocessed,
    ((EXISTS ( SELECT 1
           FROM (public.headtaillabel htl
             JOIN public.image i ON ((i.id = htl.image_id)))
          WHERE ((i.dive_id = d.id) AND i.is_canonical AND (htl.superseded = false) AND (htl.completed = true)))) AND (NOT (EXISTS ( SELECT 1
           FROM (public.headtaillabel htl
             JOIN public.image i ON ((i.id = htl.image_id)))
          WHERE ((i.dive_id = d.id) AND i.is_canonical AND (htl.superseded = false) AND ((htl.completed = false) OR (htl.completed IS NULL))))))) AS headtail_labeling_complete,
    (EXISTS ( SELECT 1
           FROM public.diveframecluster dfc
          WHERE ((dfc.dive_id = d.id) AND (dfc.data_source = 'PREDICTION'::public.datasource)))) AS has_prediction_clusters,
    ((EXISTS ( SELECT 1
           FROM (public.laserlabel ll
             JOIN public.image i ON ((i.id = ll.image_id)))
          WHERE ((i.dive_id = d.id) AND i.is_canonical AND (ll.completed = true) AND (ll.superseded = false) AND (ll.x IS NOT NULL) AND (ll.y IS NOT NULL) AND (EXISTS ( SELECT 1
                   FROM (public.diveframeclusterimagemapping mm
                     JOIN public.diveframecluster dfc ON ((dfc.id = mm.dive_frame_cluster_id)))
                  WHERE ((mm.image_id = i.id) AND (dfc.data_source = 'PREDICTION'::public.datasource))))))) AND (NOT (EXISTS ( SELECT 1
           FROM (public.laserlabel ll
             JOIN public.image i ON ((i.id = ll.image_id)))
          WHERE ((i.dive_id = d.id) AND i.is_canonical AND (ll.completed = true) AND (ll.superseded = false) AND (ll.x IS NOT NULL) AND (ll.y IS NOT NULL) AND (EXISTS ( SELECT 1
                   FROM (public.diveframeclusterimagemapping mm
                     JOIN public.diveframecluster dfc ON ((dfc.id = mm.dive_frame_cluster_id)))
                  WHERE ((mm.image_id = i.id) AND (dfc.data_source = 'PREDICTION'::public.datasource)))) AND (NOT (EXISTS ( SELECT 1
                   FROM public.specieslabel sl
                  WHERE ((sl.image_id = i.id) AND (sl.label_studio_project_id IS NOT NULL) AND (sl.superseded = false)))))))))) AS dive_images_preprocessed,
    ((EXISTS ( SELECT 1
           FROM (public.specieslabel sl
             JOIN public.image i ON ((i.id = sl.image_id)))
          WHERE ((i.dive_id = d.id) AND i.is_canonical AND (sl.superseded = false) AND (sl.completed = true)))) AND (NOT (EXISTS ( SELECT 1
           FROM (public.specieslabel sl
             JOIN public.image i ON ((i.id = sl.image_id)))
          WHERE ((i.dive_id = d.id) AND i.is_canonical AND (sl.superseded = false) AND ((sl.completed = false) OR (sl.completed IS NULL))))))) AS species_labeling_complete,
    (dive_slate_id IS NOT NULL) AS slate_applicable,
    ((dive_slate_id IS NOT NULL) AND (EXISTS ( SELECT 1
           FROM (public.specieslabel sl
             JOIN public.image i ON ((i.id = sl.image_id)))
          WHERE ((i.dive_id = d.id) AND i.is_canonical AND ((sl.content_of_image)::text = 'Slate, Laser on slate'::text)))) AND (NOT (EXISTS ( SELECT 1
           FROM (public.specieslabel sl
             JOIN public.image i ON ((i.id = sl.image_id)))
          WHERE ((i.dive_id = d.id) AND i.is_canonical AND ((sl.content_of_image)::text = 'Slate, Laser on slate'::text) AND (NOT (EXISTS ( SELECT 1
                   FROM public.diveslatelabel dsl
                  WHERE ((dsl.image_id = i.id) AND (dsl.label_studio_project_id IS NOT NULL)))))))))) AS slate_preprocessed,
    ((EXISTS ( SELECT 1
           FROM (public.diveslatelabel dsl
             JOIN public.image i ON ((i.id = dsl.image_id)))
          WHERE ((i.dive_id = d.id) AND i.is_canonical AND (dsl.superseded = false) AND (dsl.completed = true)))) AND (NOT (EXISTS ( SELECT 1
           FROM (public.diveslatelabel dsl
             JOIN public.image i ON ((i.id = dsl.image_id)))
          WHERE ((i.dive_id = d.id) AND i.is_canonical AND (dsl.superseded = false) AND ((dsl.completed = false) OR (dsl.completed IS NULL))))))) AS slate_labeling_complete,
    ((EXISTS ( SELECT 1
           FROM public.laserextrinsics le
          WHERE (le.dive_id = d.id))) OR (EXISTS ( SELECT 1
           FROM public.laserextrinsics le
          WHERE (le.dive_id = d.calibration_dive_id)))) AS calibrated,
        CASE
            WHEN (EXISTS ( SELECT 1
               FROM public.laserextrinsics le
              WHERE (le.dive_id = d.id))) THEN 'own'::text
            WHEN (EXISTS ( SELECT 1
               FROM public.laserextrinsics le
              WHERE (le.dive_id = d.calibration_dive_id))) THEN 'borrowed'::text
            ELSE 'none'::text
        END AS calibration_source,
    ((EXISTS ( SELECT 1
           FROM (public.measurement m
             JOIN public.image i ON ((i.id = m.image_id)))
          WHERE ((i.dive_id = d.id) AND i.is_canonical))) AND (NOT (EXISTS ( SELECT 1
           FROM (public.specieslabel sl
             JOIN public.image i ON ((i.id = sl.image_id)))
          WHERE ((i.dive_id = d.id) AND i.is_canonical AND (sl.top_three_photos_of_group = true) AND (((sl.content_of_image)::text ~~ '%(%)'::text) OR ((((sl.content_of_image)::text ~~ 'Fish Model,%'::text) AND (TRIM(BOTH FROM sl.content_of_image) <> 'Fish Model,'::text)) OR ((sl.content_of_image)::text = ANY (ARRAY[('Calibration Targets, Ruler'::character varying)::text, ('Calibration Targets, Box'::character varying)::text])))) AND (EXISTS ( SELECT 1
                   FROM public.laserlabel ll
                  WHERE ((ll.image_id = i.id) AND (ll.completed = true) AND (ll.superseded = false) AND (ll.x IS NOT NULL) AND (ll.y IS NOT NULL)))) AND (EXISTS ( SELECT 1
                   FROM public.headtaillabel htl
                  WHERE ((htl.image_id = i.id) AND (htl.completed = true) AND (htl.superseded = false) AND (htl.head_x IS NOT NULL) AND (htl.head_y IS NOT NULL) AND (htl.tail_x IS NOT NULL) AND (htl.tail_y IS NOT NULL)))) AND ((EXISTS ( SELECT 1
                   FROM (public.diveframeclusterimagemapping mm
                     JOIN public.diveframecluster dfc ON ((dfc.id = mm.dive_frame_cluster_id)))
                  WHERE ((mm.image_id = i.id) AND (dfc.data_source = 'LABEL_STUDIO'::public.datasource)))) OR ((((sl.content_of_image)::text ~~ 'Fish Model,%'::text) AND (TRIM(BOTH FROM sl.content_of_image) <> 'Fish Model,'::text)) OR ((sl.content_of_image)::text = ANY (ARRAY[('Calibration Targets, Ruler'::character varying)::text, ('Calibration Targets, Box'::character varying)::text])))) AND (NOT (EXISTS ( SELECT 1
                   FROM public.measurement m
                  WHERE ((m.image_id = i.id) AND (m.laser_extrinsics_id = COALESCE(( SELECT le.id
                           FROM public.laserextrinsics le
                          WHERE (le.dive_id = d.id)), ( SELECT le.id
                           FROM public.laserextrinsics le
                          WHERE (le.dive_id = d.calibration_dive_id))))))))))))) AS measured
   FROM public.dive d;


--
-- Name: diveframecluster_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.diveframecluster_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: diveframecluster_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.diveframecluster_id_seq OWNED BY public.diveframecluster.id;


--
-- Name: divelaserline; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.divelaserline (
    id integer NOT NULL,
    dive_id integer,
    a double precision NOT NULL,
    b double precision NOT NULL,
    c double precision NOT NULL,
    n_points integer NOT NULL,
    inlier_count integer NOT NULL,
    inlier_fraction double precision NOT NULL,
    residual_std double precision NOT NULL,
    label_noise_mad double precision NOT NULL,
    line_confidence double precision NOT NULL,
    fitted_at timestamp with time zone DEFAULT now()
);


--
-- Name: divelaserline_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.divelaserline_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: divelaserline_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.divelaserline_id_seq OWNED BY public.divelaserline.id;


--
-- Name: diveslate; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.diveslate (
    id integer NOT NULL,
    name character varying(100) NOT NULL,
    path character varying(255) NOT NULL,
    created_at timestamp with time zone,
    dpi integer,
    reference_points json
);


--
-- Name: diveslate_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.diveslate_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: diveslate_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.diveslate_id_seq OWNED BY public.diveslate.id;


--
-- Name: diveslatelabel_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.diveslatelabel_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: diveslatelabel_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.diveslatelabel_id_seq OWNED BY public.diveslatelabel.id;


--
-- Name: fish; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fish (
    id integer NOT NULL,
    species_id integer,
    name character varying
);


--
-- Name: fish_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.fish_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: fish_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.fish_id_seq OWNED BY public.fish.id;


--
-- Name: fish_length_estimate; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.fish_length_estimate AS
 WITH ranked AS (
         SELECT m.length_m,
            i.dive_id,
            f.id AS fish_id,
            f.name AS model_name,
            f.species_id,
            row_number() OVER (PARTITION BY f.id, i.dive_id ORDER BY m.length_m) AS rn,
            count(*) OVER (PARTITION BY f.id, i.dive_id) AS n
           FROM ((public.measurement m
             JOIN public.image i ON ((i.id = m.image_id)))
             JOIN public.fish f ON ((f.id = m.fish_id)))
          WHERE (m.length_m IS NOT NULL)
        )
 SELECT fish_id,
    dive_id,
    model_name,
    species_id,
    n AS n_frames,
    max(
        CASE
            WHEN (rn = (((9 * n) + 9) / 10)) THEN length_m
            ELSE NULL::double precision
        END) AS length_p90_m,
    max(
        CASE
            WHEN (rn = ((n + 1) / 2)) THEN length_m
            ELSE NULL::double precision
        END) AS length_median_m,
    max(length_m) AS length_max_m,
    min(length_m) AS length_min_m,
    avg(length_m) AS length_mean_m
   FROM ranked
  GROUP BY fish_id, dive_id, model_name, species_id, n;


--
-- Name: fishmodelreference; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fishmodelreference (
    id integer NOT NULL,
    name character varying NOT NULL,
    known_length_m double precision NOT NULL,
    notes character varying,
    is_provisional boolean DEFAULT false NOT NULL
);


--
-- Name: fish_model_measurement_accuracy; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.fish_model_measurement_accuracy AS
 SELECT m.id AS measurement_id,
    m.image_id,
    i.dive_id,
    f.id AS fish_id,
    f.name AS model_name,
    r.known_length_m,
    m.length_m,
    (m.length_m - r.known_length_m) AS error_m,
    (((100.0)::double precision * (m.length_m - r.known_length_m)) / r.known_length_m) AS pct_error
   FROM (((public.measurement m
     JOIN public.image i ON ((i.id = m.image_id)))
     JOIN public.fish f ON ((f.id = m.fish_id)))
     JOIN public.fishmodelreference r ON (((r.name)::text = (f.name)::text)))
  WHERE (m.length_m IS NOT NULL);


--
-- Name: fish_model_species_mislabel_suspects; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.fish_model_species_mislabel_suspects AS
 WITH frame_fit AS (
         SELECT a_1.image_id,
            r.name AS best_fit_model,
            (((100.0)::double precision * (a_1.length_m - r.known_length_m)) / r.known_length_m) AS best_fit_pct_error,
            row_number() OVER (PARTITION BY a_1.image_id ORDER BY (abs((a_1.length_m - r.known_length_m)) / r.known_length_m)) AS rk
           FROM (public.fish_model_measurement_accuracy a_1
             CROSS JOIN public.fishmodelreference r)
          WHERE ((NOT r.is_provisional) AND (NOT ((r.name)::text = ANY (ARRAY[('Ruler'::character varying)::text, ('Box'::character varying)::text]))))
        )
 SELECT a.image_id,
    a.dive_id,
    a.model_name AS labeled_model,
    a.known_length_m,
    a.length_m,
    a.pct_error,
    f.best_fit_model,
    f.best_fit_pct_error,
        CASE
            WHEN (a.pct_error > (15.0)::double precision) THEN 'high'::text
            ELSE 'medium'::text
        END AS confidence
   FROM (public.fish_model_measurement_accuracy a
     JOIN frame_fit f ON (((f.image_id = a.image_id) AND (f.rk = 1))))
  WHERE (((f.best_fit_model)::text <> (a.model_name)::text) AND (NOT ((a.model_name)::text = ANY (ARRAY[('Ruler'::character varying)::text, ('Box'::character varying)::text]))) AND (abs(a.pct_error) > (15.0)::double precision) AND (abs(f.best_fit_pct_error) < (10.0)::double precision));


--
-- Name: fishmodelreference_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.fishmodelreference_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: fishmodelreference_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.fishmodelreference_id_seq OWNED BY public.fishmodelreference.id;


--
-- Name: headtaillabel_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.headtaillabel_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: headtaillabel_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.headtaillabel_id_seq OWNED BY public.headtaillabel.id;


--
-- Name: headtailprediction; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.headtailprediction (
    id integer NOT NULL,
    head_x double precision,
    head_y double precision,
    tail_x double precision,
    tail_y double precision,
    width integer,
    height integer,
    mask_area_px integer,
    silhouette_ratio double precision,
    crop_x integer,
    crop_y integer,
    laser_label_id integer,
    predictor_version integer,
    checkpoint character varying,
    core_version character varying,
    status character varying NOT NULL,
    rejected_low_confidence boolean NOT NULL,
    created_at timestamp with time zone,
    image_id integer
);


--
-- Name: headtailprediction_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.headtailprediction_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: headtailprediction_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.headtailprediction_id_seq OWNED BY public.headtailprediction.id;


--
-- Name: image_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.image_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: image_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.image_id_seq OWNED BY public.image.id;


--
-- Name: labelstudiosynccursor; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.labelstudiosynccursor (
    id integer NOT NULL,
    kind character varying NOT NULL,
    label_studio_project_id integer NOT NULL,
    last_synced_at timestamp with time zone
);


--
-- Name: labelstudiosynccursor_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.labelstudiosynccursor_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: labelstudiosynccursor_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.labelstudiosynccursor_id_seq OWNED BY public.labelstudiosynccursor.id;


--
-- Name: laserdepth; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.laserdepth (
    id integer NOT NULL,
    depth_m double precision NOT NULL,
    range_m double precision,
    residual_m double precision,
    created_at timestamp with time zone,
    image_id integer,
    laser_label_id integer,
    laser_extrinsics_id integer
);


--
-- Name: laserdepth_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.laserdepth_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: laserdepth_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.laserdepth_id_seq OWNED BY public.laserdepth.id;


--
-- Name: laserextrinsics_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.laserextrinsics_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: laserextrinsics_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.laserextrinsics_id_seq OWNED BY public.laserextrinsics.id;


--
-- Name: laserlabel_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.laserlabel_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: laserlabel_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.laserlabel_id_seq OWNED BY public.laserlabel.id;


--
-- Name: laserprediction; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.laserprediction (
    id integer NOT NULL,
    x double precision,
    y double precision,
    confidence double precision NOT NULL,
    width integer,
    height integer,
    created_at timestamp with time zone,
    image_id integer,
    color character varying,
    predictor_version integer,
    checkpoint character varying,
    core_version character varying,
    color_margin double precision,
    rejected_out_of_region boolean DEFAULT false NOT NULL,
    auto_accept boolean DEFAULT false NOT NULL,
    gate_verdict character varying,
    line_offset_px double precision,
    line_position_z double precision
);


--
-- Name: laserprediction_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.laserprediction_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: laserprediction_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.laserprediction_id_seq OWNED BY public.laserprediction.id;


--
-- Name: measurement_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.measurement_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: measurement_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.measurement_id_seq OWNED BY public.measurement.id;


--
-- Name: slateprediction; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.slateprediction (
    id integer NOT NULL,
    reference_points json,
    confidence double precision NOT NULL,
    rejected_reason character varying,
    width integer,
    height integer,
    created_at timestamp with time zone,
    image_id integer
);


--
-- Name: slateprediction_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.slateprediction_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: slateprediction_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.slateprediction_id_seq OWNED BY public.slateprediction.id;


--
-- Name: species; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.species (
    id integer NOT NULL,
    scientific_name character varying,
    common_name character varying
);


--
-- Name: species_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.species_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: species_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.species_id_seq OWNED BY public.species.id;


--
-- Name: specieslabel_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.specieslabel_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: specieslabel_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.specieslabel_id_seq OWNED BY public.specieslabel.id;


--
-- Name: user; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."user" (
    id integer NOT NULL,
    email character varying(100),
    first_name character varying(100),
    last_name character varying(100),
    last_activity timestamp with time zone,
    date_joined timestamp with time zone,
    label_studio_id integer
);


--
-- Name: user_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.user_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: user_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.user_id_seq OWNED BY public."user".id;


--
-- Name: calibrationtarget id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.calibrationtarget ALTER COLUMN id SET DEFAULT nextval('public.calibrationtarget_id_seq'::regclass);


--
-- Name: camera id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.camera ALTER COLUMN id SET DEFAULT nextval('public.camera_id_seq'::regclass);


--
-- Name: cameraintrinsics id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cameraintrinsics ALTER COLUMN id SET DEFAULT nextval('public.cameraintrinsics_id_seq'::regclass);


--
-- Name: dive id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dive ALTER COLUMN id SET DEFAULT nextval('public.dive_id_seq'::regclass);


--
-- Name: diveframecluster id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diveframecluster ALTER COLUMN id SET DEFAULT nextval('public.diveframecluster_id_seq'::regclass);


--
-- Name: divelaserline id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.divelaserline ALTER COLUMN id SET DEFAULT nextval('public.divelaserline_id_seq'::regclass);


--
-- Name: diveslate id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diveslate ALTER COLUMN id SET DEFAULT nextval('public.diveslate_id_seq'::regclass);


--
-- Name: diveslatelabel id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diveslatelabel ALTER COLUMN id SET DEFAULT nextval('public.diveslatelabel_id_seq'::regclass);


--
-- Name: fish id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fish ALTER COLUMN id SET DEFAULT nextval('public.fish_id_seq'::regclass);


--
-- Name: fishmodelreference id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fishmodelreference ALTER COLUMN id SET DEFAULT nextval('public.fishmodelreference_id_seq'::regclass);


--
-- Name: headtaillabel id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.headtaillabel ALTER COLUMN id SET DEFAULT nextval('public.headtaillabel_id_seq'::regclass);


--
-- Name: headtailprediction id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.headtailprediction ALTER COLUMN id SET DEFAULT nextval('public.headtailprediction_id_seq'::regclass);


--
-- Name: image id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.image ALTER COLUMN id SET DEFAULT nextval('public.image_id_seq'::regclass);


--
-- Name: labelstudiosynccursor id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.labelstudiosynccursor ALTER COLUMN id SET DEFAULT nextval('public.labelstudiosynccursor_id_seq'::regclass);


--
-- Name: laserdepth id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.laserdepth ALTER COLUMN id SET DEFAULT nextval('public.laserdepth_id_seq'::regclass);


--
-- Name: laserextrinsics id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.laserextrinsics ALTER COLUMN id SET DEFAULT nextval('public.laserextrinsics_id_seq'::regclass);


--
-- Name: laserlabel id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.laserlabel ALTER COLUMN id SET DEFAULT nextval('public.laserlabel_id_seq'::regclass);


--
-- Name: laserprediction id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.laserprediction ALTER COLUMN id SET DEFAULT nextval('public.laserprediction_id_seq'::regclass);


--
-- Name: measurement id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.measurement ALTER COLUMN id SET DEFAULT nextval('public.measurement_id_seq'::regclass);


--
-- Name: slateprediction id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.slateprediction ALTER COLUMN id SET DEFAULT nextval('public.slateprediction_id_seq'::regclass);


--
-- Name: species id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.species ALTER COLUMN id SET DEFAULT nextval('public.species_id_seq'::regclass);


--
-- Name: specieslabel id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.specieslabel ALTER COLUMN id SET DEFAULT nextval('public.specieslabel_id_seq'::regclass);


--
-- Name: user id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."user" ALTER COLUMN id SET DEFAULT nextval('public.user_id_seq'::regclass);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: calibrationtarget calibrationtarget_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.calibrationtarget
    ADD CONSTRAINT calibrationtarget_pkey PRIMARY KEY (id);


--
-- Name: camera camera_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.camera
    ADD CONSTRAINT camera_pkey PRIMARY KEY (id);


--
-- Name: cameraintrinsics cameraintrinsics_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cameraintrinsics
    ADD CONSTRAINT cameraintrinsics_pkey PRIMARY KEY (id);


--
-- Name: dive dive_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dive
    ADD CONSTRAINT dive_pkey PRIMARY KEY (id);


--
-- Name: diveframecluster diveframecluster_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diveframecluster
    ADD CONSTRAINT diveframecluster_pkey PRIMARY KEY (id);


--
-- Name: diveframeclusterimagemapping diveframeclusterimagemapping_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diveframeclusterimagemapping
    ADD CONSTRAINT diveframeclusterimagemapping_pkey PRIMARY KEY (dive_frame_cluster_id, image_id);


--
-- Name: divelaserline divelaserline_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.divelaserline
    ADD CONSTRAINT divelaserline_pkey PRIMARY KEY (id);


--
-- Name: diveslate diveslate_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diveslate
    ADD CONSTRAINT diveslate_pkey PRIMARY KEY (id);


--
-- Name: diveslatelabel diveslatelabel_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diveslatelabel
    ADD CONSTRAINT diveslatelabel_pkey PRIMARY KEY (id);


--
-- Name: fish fish_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fish
    ADD CONSTRAINT fish_pkey PRIMARY KEY (id);


--
-- Name: fishmodelreference fishmodelreference_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fishmodelreference
    ADD CONSTRAINT fishmodelreference_pkey PRIMARY KEY (id);


--
-- Name: headtaillabel headtaillabel_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.headtaillabel
    ADD CONSTRAINT headtaillabel_pkey PRIMARY KEY (id);


--
-- Name: headtailprediction headtailprediction_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.headtailprediction
    ADD CONSTRAINT headtailprediction_pkey PRIMARY KEY (id);


--
-- Name: image image_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.image
    ADD CONSTRAINT image_pkey PRIMARY KEY (id);


--
-- Name: labelstudiosynccursor labelstudiosynccursor_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.labelstudiosynccursor
    ADD CONSTRAINT labelstudiosynccursor_pkey PRIMARY KEY (id);


--
-- Name: laserdepth laserdepth_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.laserdepth
    ADD CONSTRAINT laserdepth_pkey PRIMARY KEY (id);


--
-- Name: laserextrinsics laserextrinsics_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.laserextrinsics
    ADD CONSTRAINT laserextrinsics_pkey PRIMARY KEY (id);


--
-- Name: laserlabel laserlabel_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.laserlabel
    ADD CONSTRAINT laserlabel_pkey PRIMARY KEY (id);


--
-- Name: laserprediction laserprediction_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.laserprediction
    ADD CONSTRAINT laserprediction_pkey PRIMARY KEY (id);


--
-- Name: measurement measurement_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.measurement
    ADD CONSTRAINT measurement_pkey PRIMARY KEY (id);


--
-- Name: slateprediction slateprediction_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.slateprediction
    ADD CONSTRAINT slateprediction_pkey PRIMARY KEY (id);


--
-- Name: species species_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.species
    ADD CONSTRAINT species_pkey PRIMARY KEY (id);


--
-- Name: specieslabel specieslabel_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.specieslabel
    ADD CONSTRAINT specieslabel_pkey PRIMARY KEY (id);


--
-- Name: camera uq_camera_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.camera
    ADD CONSTRAINT uq_camera_name UNIQUE (name);


--
-- Name: camera uq_camera_serial_number; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.camera
    ADD CONSTRAINT uq_camera_serial_number UNIQUE (serial_number);


--
-- Name: diveslatelabel uq_dive_slate_image_project; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diveslatelabel
    ADD CONSTRAINT uq_dive_slate_image_project UNIQUE (image_id, label_studio_project_id);


--
-- Name: divelaserline uq_divelaserline_dive_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.divelaserline
    ADD CONSTRAINT uq_divelaserline_dive_id UNIQUE (dive_id);


--
-- Name: fishmodelreference uq_fish_model_reference_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fishmodelreference
    ADD CONSTRAINT uq_fish_model_reference_name UNIQUE (name);


--
-- Name: fish uq_fish_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fish
    ADD CONSTRAINT uq_fish_name UNIQUE (name);


--
-- Name: headtaillabel uq_headtail_image_project; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.headtaillabel
    ADD CONSTRAINT uq_headtail_image_project UNIQUE (image_id, label_studio_project_id);


--
-- Name: headtailprediction uq_headtail_prediction_image; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.headtailprediction
    ADD CONSTRAINT uq_headtail_prediction_image UNIQUE (image_id);


--
-- Name: labelstudiosynccursor uq_labelstudiosynccursor_kind_project; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.labelstudiosynccursor
    ADD CONSTRAINT uq_labelstudiosynccursor_kind_project UNIQUE (kind, label_studio_project_id);


--
-- Name: laserdepth uq_laser_depth_image; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.laserdepth
    ADD CONSTRAINT uq_laser_depth_image UNIQUE (image_id);


--
-- Name: laserlabel uq_laser_image_project; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.laserlabel
    ADD CONSTRAINT uq_laser_image_project UNIQUE (image_id, label_studio_project_id);


--
-- Name: laserprediction uq_laser_prediction_image; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.laserprediction
    ADD CONSTRAINT uq_laser_prediction_image UNIQUE (image_id);


--
-- Name: laserextrinsics uq_laserextrinsics_dive_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.laserextrinsics
    ADD CONSTRAINT uq_laserextrinsics_dive_id UNIQUE (dive_id);


--
-- Name: measurement uq_measurement_image_fish; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.measurement
    ADD CONSTRAINT uq_measurement_image_fish UNIQUE (image_id, fish_id);


--
-- Name: slateprediction uq_slate_prediction_image; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.slateprediction
    ADD CONSTRAINT uq_slate_prediction_image UNIQUE (image_id);


--
-- Name: specieslabel uq_species_image_project; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.specieslabel
    ADD CONSTRAINT uq_species_image_project UNIQUE (image_id, label_studio_project_id);


--
-- Name: user uq_user_email; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."user"
    ADD CONSTRAINT uq_user_email UNIQUE (email);


--
-- Name: user uq_user_label_studio_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."user"
    ADD CONSTRAINT uq_user_label_studio_id UNIQUE (label_studio_id);


--
-- Name: user user_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."user"
    ADD CONSTRAINT user_pkey PRIMARY KEY (id);


--
-- Name: ix_calibrationtarget_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_calibrationtarget_name ON public.calibrationtarget USING btree (name);


--
-- Name: ix_camera_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_camera_name ON public.camera USING btree (name);


--
-- Name: ix_camera_serial_number; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_camera_serial_number ON public.camera USING btree (serial_number);


--
-- Name: ix_dive_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dive_name ON public.dive USING btree (name);


--
-- Name: ix_dive_path; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_dive_path ON public.dive USING btree (path);


--
-- Name: ix_diveslate_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_diveslate_name ON public.diveslate USING btree (name);


--
-- Name: ix_diveslate_path; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_diveslate_path ON public.diveslate USING btree (path);


--
-- Name: ix_diveslatelabel_label_studio_project_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_diveslatelabel_label_studio_project_id ON public.diveslatelabel USING btree (label_studio_project_id);


--
-- Name: ix_diveslatelabel_label_studio_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_diveslatelabel_label_studio_task_id ON public.diveslatelabel USING btree (label_studio_task_id);


--
-- Name: ix_fishmodelreference_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_fishmodelreference_name ON public.fishmodelreference USING btree (name);


--
-- Name: ix_headtaillabel_label_studio_project_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_headtaillabel_label_studio_project_id ON public.headtaillabel USING btree (label_studio_project_id);


--
-- Name: ix_headtaillabel_label_studio_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_headtaillabel_label_studio_task_id ON public.headtaillabel USING btree (label_studio_task_id);


--
-- Name: ix_headtailprediction_predictor_version; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_headtailprediction_predictor_version ON public.headtailprediction USING btree (predictor_version);


--
-- Name: ix_image_checksum; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_image_checksum ON public.image USING btree (checksum);


--
-- Name: ix_image_path; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_image_path ON public.image USING btree (path);


--
-- Name: ix_labelstudiosynccursor_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_labelstudiosynccursor_kind ON public.labelstudiosynccursor USING btree (kind);


--
-- Name: ix_labelstudiosynccursor_label_studio_project_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_labelstudiosynccursor_label_studio_project_id ON public.labelstudiosynccursor USING btree (label_studio_project_id);


--
-- Name: ix_laserlabel_label_studio_project_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_laserlabel_label_studio_project_id ON public.laserlabel USING btree (label_studio_project_id);


--
-- Name: ix_laserlabel_label_studio_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_laserlabel_label_studio_task_id ON public.laserlabel USING btree (label_studio_task_id);


--
-- Name: ix_laserprediction_gate_verdict; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_laserprediction_gate_verdict ON public.laserprediction USING btree (gate_verdict);


--
-- Name: ix_laserprediction_predictor_version; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_laserprediction_predictor_version ON public.laserprediction USING btree (predictor_version);


--
-- Name: ix_species_common_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_species_common_name ON public.species USING btree (common_name);


--
-- Name: ix_species_scientific_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_species_scientific_name ON public.species USING btree (scientific_name);


--
-- Name: ix_specieslabel_label_studio_project_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_specieslabel_label_studio_project_id ON public.specieslabel USING btree (label_studio_project_id);


--
-- Name: ix_specieslabel_label_studio_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_specieslabel_label_studio_task_id ON public.specieslabel USING btree (label_studio_task_id);


--
-- Name: ix_user_email; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_user_email ON public."user" USING btree (email);


--
-- Name: ix_user_label_studio_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_user_label_studio_id ON public."user" USING btree (label_studio_id);


--
-- Name: uq_image_canonical_checksum; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_image_canonical_checksum ON public.image USING btree (checksum) WHERE is_canonical;


--
-- Name: cameraintrinsics cameraintrinsics_camera_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cameraintrinsics
    ADD CONSTRAINT cameraintrinsics_camera_id_fkey FOREIGN KEY (camera_id) REFERENCES public.camera(id);


--
-- Name: dive dive_camera_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dive
    ADD CONSTRAINT dive_camera_id_fkey FOREIGN KEY (camera_id) REFERENCES public.camera(id);


--
-- Name: dive dive_dive_slate_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dive
    ADD CONSTRAINT dive_dive_slate_id_fkey FOREIGN KEY (dive_slate_id) REFERENCES public.diveslate(id);


--
-- Name: diveframecluster diveframecluster_dive_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diveframecluster
    ADD CONSTRAINT diveframecluster_dive_id_fkey FOREIGN KEY (dive_id) REFERENCES public.dive(id);


--
-- Name: diveframecluster diveframecluster_fish_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diveframecluster
    ADD CONSTRAINT diveframecluster_fish_id_fkey FOREIGN KEY (fish_id) REFERENCES public.fish(id);


--
-- Name: diveframeclusterimagemapping diveframeclusterimagemapping_dive_frame_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diveframeclusterimagemapping
    ADD CONSTRAINT diveframeclusterimagemapping_dive_frame_cluster_id_fkey FOREIGN KEY (dive_frame_cluster_id) REFERENCES public.diveframecluster(id);


--
-- Name: diveframeclusterimagemapping diveframeclusterimagemapping_image_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diveframeclusterimagemapping
    ADD CONSTRAINT diveframeclusterimagemapping_image_id_fkey FOREIGN KEY (image_id) REFERENCES public.image(id);


--
-- Name: divelaserline divelaserline_dive_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.divelaserline
    ADD CONSTRAINT divelaserline_dive_id_fkey FOREIGN KEY (dive_id) REFERENCES public.dive(id);


--
-- Name: diveslatelabel diveslatelabel_image_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diveslatelabel
    ADD CONSTRAINT diveslatelabel_image_id_fkey FOREIGN KEY (image_id) REFERENCES public.image(id);


--
-- Name: diveslatelabel diveslatelabel_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.diveslatelabel
    ADD CONSTRAINT diveslatelabel_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id);


--
-- Name: fish fish_species_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fish
    ADD CONSTRAINT fish_species_id_fkey FOREIGN KEY (species_id) REFERENCES public.species(id);


--
-- Name: dive fk_dive_calibration_dive_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dive
    ADD CONSTRAINT fk_dive_calibration_dive_id FOREIGN KEY (calibration_dive_id) REFERENCES public.dive(id);


--
-- Name: dive fk_dive_calibration_target_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dive
    ADD CONSTRAINT fk_dive_calibration_target_id FOREIGN KEY (calibration_target_id) REFERENCES public.calibrationtarget(id);


--
-- Name: measurement fk_measurement_laser_extrinsics_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.measurement
    ADD CONSTRAINT fk_measurement_laser_extrinsics_id FOREIGN KEY (laser_extrinsics_id) REFERENCES public.laserextrinsics(id);


--
-- Name: headtaillabel headtaillabel_image_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.headtaillabel
    ADD CONSTRAINT headtaillabel_image_id_fkey FOREIGN KEY (image_id) REFERENCES public.image(id);


--
-- Name: headtaillabel headtaillabel_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.headtaillabel
    ADD CONSTRAINT headtaillabel_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id);


--
-- Name: headtailprediction headtailprediction_image_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.headtailprediction
    ADD CONSTRAINT headtailprediction_image_id_fkey FOREIGN KEY (image_id) REFERENCES public.image(id);


--
-- Name: headtailprediction headtailprediction_laser_label_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.headtailprediction
    ADD CONSTRAINT headtailprediction_laser_label_id_fkey FOREIGN KEY (laser_label_id) REFERENCES public.laserlabel(id);


--
-- Name: image image_camera_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.image
    ADD CONSTRAINT image_camera_id_fkey FOREIGN KEY (camera_id) REFERENCES public.camera(id);


--
-- Name: image image_dive_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.image
    ADD CONSTRAINT image_dive_id_fkey FOREIGN KEY (dive_id) REFERENCES public.dive(id);


--
-- Name: laserdepth laserdepth_image_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.laserdepth
    ADD CONSTRAINT laserdepth_image_id_fkey FOREIGN KEY (image_id) REFERENCES public.image(id);


--
-- Name: laserdepth laserdepth_laser_extrinsics_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.laserdepth
    ADD CONSTRAINT laserdepth_laser_extrinsics_id_fkey FOREIGN KEY (laser_extrinsics_id) REFERENCES public.laserextrinsics(id);


--
-- Name: laserdepth laserdepth_laser_label_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.laserdepth
    ADD CONSTRAINT laserdepth_laser_label_id_fkey FOREIGN KEY (laser_label_id) REFERENCES public.laserlabel(id);


--
-- Name: laserextrinsics laserextrinsics_camera_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.laserextrinsics
    ADD CONSTRAINT laserextrinsics_camera_id_fkey FOREIGN KEY (camera_id) REFERENCES public.camera(id);


--
-- Name: laserextrinsics laserextrinsics_dive_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.laserextrinsics
    ADD CONSTRAINT laserextrinsics_dive_id_fkey FOREIGN KEY (dive_id) REFERENCES public.dive(id);


--
-- Name: laserlabel laserlabel_image_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.laserlabel
    ADD CONSTRAINT laserlabel_image_id_fkey FOREIGN KEY (image_id) REFERENCES public.image(id);


--
-- Name: laserlabel laserlabel_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.laserlabel
    ADD CONSTRAINT laserlabel_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id);


--
-- Name: laserprediction laserprediction_image_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.laserprediction
    ADD CONSTRAINT laserprediction_image_id_fkey FOREIGN KEY (image_id) REFERENCES public.image(id);


--
-- Name: measurement measurement_fish_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.measurement
    ADD CONSTRAINT measurement_fish_id_fkey FOREIGN KEY (fish_id) REFERENCES public.fish(id);


--
-- Name: measurement measurement_image_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.measurement
    ADD CONSTRAINT measurement_image_id_fkey FOREIGN KEY (image_id) REFERENCES public.image(id);


--
-- Name: slateprediction slateprediction_image_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.slateprediction
    ADD CONSTRAINT slateprediction_image_id_fkey FOREIGN KEY (image_id) REFERENCES public.image(id);


--
-- Name: specieslabel specieslabel_image_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.specieslabel
    ADD CONSTRAINT specieslabel_image_id_fkey FOREIGN KEY (image_id) REFERENCES public.image(id);


--
-- Name: specieslabel specieslabel_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.specieslabel
    ADD CONSTRAINT specieslabel_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(id);


--
-- PostgreSQL database dump complete
--


